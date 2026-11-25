"""SQLite 台账与内存实现。

所有写操作在同一个连接上串行提交，``allocations`` 表的部分唯一索引保证
同一车次的同一物理空间在任何“仍占用”状态下不会被分配两次——包括承运方
占座成功后断线、本地只能标记为结果未明的情形。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from .domain import AllocState, AllocationView, CaseState, LegConfig, Member, Need, OCCUPYING_STATES, Space, SpaceKind, parse_time

SCHEMA = """
CREATE TABLE IF NOT EXISTS legs (
    leg_id TEXT PRIMARY KEY,
    carrier TEXT NOT NULL,
    step_free INTEGER NOT NULL,
    spaces_json TEXT NOT NULL,
    groups_json TEXT NOT NULL,
    boarding_cutoff TEXT,
    arrives TEXT,
    cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    party_id TEXT,
    itinerary_json TEXT NOT NULL,
    state TEXT NOT NULL,
    split_allowed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    decision_note TEXT
);
CREATE TABLE IF NOT EXISTS case_members (
    case_id TEXT NOT NULL,
    passenger_id TEXT NOT NULL,
    need TEXT NOT NULL,
    credential TEXT NOT NULL,
    PRIMARY KEY (case_id, passenger_id)
);
CREATE TABLE IF NOT EXISTS allocations (
    allocation_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    passenger_id TEXT NOT NULL,
    leg_id TEXT NOT NULL,
    space_label TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    credential TEXT NOT NULL,
    request_op_id TEXT,
    remote_hold TEXT,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_occupant_per_space
    ON allocations(leg_id, space_label)
    WHERE state IN ('pre_claimed','held','boarded','releasable');
CREATE TABLE IF NOT EXISTS boarding_facts (
    scan_id TEXT PRIMARY KEY,
    passenger_id TEXT NOT NULL,
    leg_id TEXT NOT NULL,
    credential TEXT NOT NULL,
    physical_space TEXT,
    at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_boarding_per_passenger_leg
    ON boarding_facts(passenger_id, leg_id);
CREATE TABLE IF NOT EXISTS passenger_decisions (
    case_id TEXT NOT NULL,
    passenger_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    split_allowed INTEGER NOT NULL DEFAULT 0,
    at TEXT NOT NULL,
    PRIMARY KEY (case_id, passenger_id)
);
CREATE TABLE IF NOT EXISTS external_occupancy (
    leg_id TEXT NOT NULL,
    space_label TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT,
    at TEXT NOT NULL,
    PRIMARY KEY (leg_id, space_label)
);
CREATE TABLE IF NOT EXISTS carrier_ops (
    op_id TEXT PRIMARY KEY,
    carrier TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_json TEXT NOT NULL,
    state TEXT NOT NULL,
    remote_ref TEXT,
    reply_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    request_id TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    at TEXT NOT NULL
);
"""

OCCUPYING_SQL = ("'pre_claimed','held','boarded','releasable'",)


class Conflict(Exception):
    """唯一约束冲突：物理空间已被占用或请求编号被复用。"""


def _now_iso(now: Optional[datetime] = None) -> str:
    if now is None:
        from datetime import timezone

        now = datetime.now(timezone.utc)
    return now.isoformat()


class Repository:
    """台账端口。``SqliteRepository`` 与 ``MemoryRepository`` 实现同一接口。"""

    # --- 车次配置 ---
    def upsert_leg(self, leg: LegConfig) -> None: ...
    def get_leg(self, leg_id: str) -> Optional[LegConfig]: ...
    def list_legs(self) -> list[LegConfig]: ...
    def mark_leg_cancelled(self, leg_id: str, cancelled: bool) -> None: ...
    def record_leg_delay(self, leg_id: str, arrives: datetime) -> None: ...

    # --- 改签方案 ---
    def create_case(
        self,
        case_id: str,
        party_id: Optional[str],
        itinerary: list[str],
        members: list[Member],
        split_allowed: bool,
        now: datetime,
    ) -> None: ...
    def update_case_itinerary(self, case_id: str, itinerary: list[str], now: datetime, note: Optional[str] = None) -> None: ...
    def update_case_state(self, case_id: str, state: CaseState, now: datetime, note: Optional[str] = None) -> None: ...
    def get_case(self, case_id: str) -> Optional[dict]: ...
    def list_cases(self) -> list[dict]: ...
    def list_members(self, case_id: str) -> list[Member]: ...

    # --- 座位安排 ---
    def add_allocation(self, allocation: AllocationView, now: datetime) -> None: ...
    def update_allocation_state(
        self, allocation_id: str, state: AllocState, now: datetime,
        remote_hold: Optional[str] = None, request_op_id: Optional[str] = None,
    ) -> None: ...
    def list_allocations(self, case_id: Optional[str] = None) -> list[AllocationView]: ...
    def get_allocation(self, allocation_id: str) -> Optional[AllocationView]: ...
    def find_allocation(self, case_id: str, passenger_id: str, leg_id: str) -> Optional[AllocationView]: ...

    # --- 登车事实 / 外部占座 ---
    def record_boarding(
        self, scan_id: str, passenger_id: str, leg_id: str,
        credential: str, physical_space: Optional[str], at: datetime,
    ) -> bool: ...
    def get_boarding(self, passenger_id: str, leg_id: str) -> Optional[dict]: ...
    def list_boarding(self, leg_id: Optional[str] = None) -> list[dict]: ...

    # --- 旅客决定（接受保障/保留原确认/同意拆分）---
    def record_decision(
        self, case_id: str, passenger_id: str, decision: str,
        split_allowed: bool, at: datetime,
    ) -> None: ...
    def list_decisions(self, case_id: str) -> list[dict]: ...
    def upsert_external_occupancy(
        self, leg_id: str, space_label: str, state: str, reason: Optional[str], at: datetime,
    ) -> None: ...
    def list_external_occupancy(self, leg_id: Optional[str] = None) -> list[dict]: ...

    # --- 承运方操作与幂等 ---
    def upsert_carrier_op(self, op: dict, now: datetime) -> None: ...
    def get_carrier_op(self, op_id: str) -> Optional[dict]: ...
    def list_carrier_ops(self, state: Optional[str] = None) -> list[dict]: ...
    def store_idempotent(self, request_id: str, response: dict, now: datetime) -> None: ...
    def get_idempotent(self, request_id: str) -> Optional[dict]: ...


def _row_to_leg(row: sqlite3.Row | tuple) -> LegConfig:
    if isinstance(row, sqlite3.Row):
        values = tuple(row)
    else:
        values = row
    (leg_id, carrier, step_free, spaces_json, groups_json,
     cutoff, arrives, cancelled) = values
    spaces = {
        label: Space(leg_id, label, SpaceKind(kind))
        for label, kind in json.loads(spaces_json).items()
    }
    groups = [frozenset(group) for group in json.loads(groups_json)]
    return LegConfig(
        leg_id=leg_id,
        carrier=carrier,
        step_free=bool(step_free),
        spaces=spaces,
        conflict_groups=groups,
        boarding_cutoff=parse_time(cutoff) if cutoff else None,
        arrives=parse_time(arrives) if arrives else None,
        cancelled=bool(cancelled),
    )


def _row_to_allocation(row: sqlite3.Row | tuple) -> AllocationView:
    if isinstance(row, sqlite3.Row):
        values = tuple(row)
    else:
        values = row
    (allocation_id, case_id, passenger_id, leg_id, space_label,
     kind, state, credential, request_op_id, remote_hold, _updated) = values
    return AllocationView(
        allocation_id=allocation_id,
        case_id=case_id,
        passenger_id=passenger_id,
        leg_id=leg_id,
        space_label=space_label,
        kind=SpaceKind(kind),
        state=AllocState(state),
        credential=credential,
        request_op_id=request_op_id,
        remote_hold=remote_hold,
    )


class SqliteRepository(Repository):
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                yield self
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    # --- 车次 ---
    def upsert_leg(self, leg: LegConfig) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO legs(leg_id,carrier,step_free,spaces_json,groups_json,
                       boarding_cutoff,arrives,cancelled)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(leg_id) DO UPDATE SET
                     carrier=excluded.carrier, step_free=excluded.step_free,
                     spaces_json=excluded.spaces_json, groups_json=excluded.groups_json,
                     boarding_cutoff=excluded.boarding_cutoff, arrives=excluded.arrives,
                     cancelled=excluded.cancelled""",
                (
                    leg.leg_id, leg.carrier, int(leg.step_free),
                    json.dumps({label: space.kind.value for label, space in leg.spaces.items()}),
                    json.dumps([sorted(group) for group in leg.conflict_groups]),
                    leg.boarding_cutoff.isoformat() if leg.boarding_cutoff else None,
                    leg.arrives.isoformat() if leg.arrives else None,
                    int(leg.cancelled),
                ),
            )
            self.conn.commit()

    def get_leg(self, leg_id: str) -> Optional[LegConfig]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM legs WHERE leg_id=?", (leg_id,)).fetchone()
        return _row_to_leg(row) if row else None

    def list_legs(self) -> list[LegConfig]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM legs ORDER BY leg_id").fetchall()
        return [_row_to_leg(row) for row in rows]

    def mark_leg_cancelled(self, leg_id: str, cancelled: bool) -> None:
        with self._lock:
            self.conn.execute("UPDATE legs SET cancelled=? WHERE leg_id=?", (int(cancelled), leg_id))
            self.conn.commit()

    def record_leg_delay(self, leg_id: str, arrives: datetime) -> None:
        with self._lock:
            self.conn.execute("UPDATE legs SET arrives=? WHERE leg_id=?", (parse_time(arrives).isoformat(), leg_id))
            self.conn.commit()

    # --- 方案 ---
    def create_case(self, case_id, party_id, itinerary, members, split_allowed, now):
        with self._lock:
            try:
                self.conn.execute(
                    """INSERT INTO cases(case_id,party_id,itinerary_json,state,split_allowed,
                           created_at,updated_at) VALUES(?,?,?,?,?,?,?)""",
                    (case_id, party_id, json.dumps(itinerary), CaseState.PENDING.value,
                     int(split_allowed), _now_iso(now), _now_iso(now)),
                )
                self.conn.executemany(
                    "INSERT INTO case_members(case_id,passenger_id,need,credential) VALUES(?,?,?,?)",
                    [(case_id, m.passenger_id, m.need.value, m.credential) for m in members],
                )
                self.conn.commit()
            except sqlite3.IntegrityError as exc:
                self.conn.rollback()
                raise Conflict(str(exc)) from exc

    def update_case_itinerary(self, case_id, itinerary, now, note=None):
        with self._lock:
            self.conn.execute(
                "UPDATE cases SET itinerary_json=?, updated_at=?, decision_note=COALESCE(?,decision_note) WHERE case_id=?",
                (json.dumps(itinerary), _now_iso(now), note, case_id),
            )
            self.conn.commit()

    def update_case_state(self, case_id, state, now, note=None):
        with self._lock:
            self.conn.execute(
                "UPDATE cases SET state=?, updated_at=?, decision_note=COALESCE(?,decision_note) WHERE case_id=?",
                (state.value, _now_iso(now), note, case_id),
            )
            self.conn.commit()

    def get_case(self, case_id: str) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            return None
        return {
            "case_id": row["case_id"],
            "party_id": row["party_id"],
            "itinerary": json.loads(row["itinerary_json"]),
            "state": CaseState(row["state"]),
            "split_allowed": bool(row["split_allowed"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "decision_note": row["decision_note"],
        }

    def list_cases(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT case_id FROM cases ORDER BY created_at").fetchall()
        return [self.get_case(row["case_id"]) for row in rows]

    def list_members(self, case_id: str) -> list[Member]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT passenger_id,need,credential FROM case_members WHERE case_id=?", (case_id,)
            ).fetchall()
        return [Member(r["passenger_id"], Need(r["need"]), r["credential"]) for r in rows]

    # --- 安排 ---
    def add_allocation(self, allocation: AllocationView, now: datetime) -> None:
        with self._lock:
            try:
                self.conn.execute(
                    """INSERT INTO allocations(allocation_id,case_id,passenger_id,leg_id,
                           space_label,kind,state,credential,request_op_id,remote_hold,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (allocation.allocation_id, allocation.case_id, allocation.passenger_id,
                     allocation.leg_id, allocation.space_label, allocation.kind.value,
                     allocation.state.value, allocation.credential,
                     allocation.request_op_id, allocation.remote_hold, _now_iso(now)),
                )
                self.conn.commit()
            except sqlite3.IntegrityError as exc:
                self.conn.rollback()
                raise Conflict(f"space {allocation.leg_id}:{allocation.space_label} already occupied") from exc

    def update_allocation_state(self, allocation_id, state, now, remote_hold=None, request_op_id=None):
        with self._lock:
            current = self.conn.execute(
                "SELECT state FROM allocations WHERE allocation_id=?", (allocation_id,)
            ).fetchone()
            if current is None:
                raise Conflict(f"unknown allocation {allocation_id}")
            # RELEASED 是终态，避免清单确认被重复处理后复活。
            if current["state"] == AllocState.RELEASED.value and state is not AllocState.RELEASED:
                raise Conflict(f"allocation {allocation_id} already released")
            self.conn.execute(
                """UPDATE allocations SET state=?, remote_hold=COALESCE(?,remote_hold),
                       request_op_id=COALESCE(?,request_op_id), updated_at=?
                   WHERE allocation_id=?""",
                (state.value, remote_hold, request_op_id, _now_iso(now), allocation_id),
            )
            self.conn.commit()

    def list_allocations(self, case_id=None) -> list[AllocationView]:
        with self._lock:
            if case_id:
                rows = self.conn.execute(
                    "SELECT * FROM allocations WHERE case_id=? ORDER BY leg_id,passenger_id", (case_id,)
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM allocations ORDER BY leg_id,passenger_id"
                ).fetchall()
        return [_row_to_allocation(row) for row in rows]

    def get_allocation(self, allocation_id: str) -> Optional[AllocationView]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM allocations WHERE allocation_id=?", (allocation_id,)
            ).fetchone()
        return _row_to_allocation(row) if row else None

    def find_allocation(self, case_id, passenger_id, leg_id) -> Optional[AllocationView]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM allocations WHERE case_id=? AND passenger_id=? AND leg_id=?",
                (case_id, passenger_id, leg_id),
            ).fetchone()
        return _row_to_allocation(row) if row else None

    # --- 登车 / 外部占座 ---
    def record_boarding(self, scan_id, passenger_id, leg_id, credential, physical_space, at) -> bool:
        with self._lock:
            try:
                self.conn.execute(
                    """INSERT INTO boarding_facts(scan_id,passenger_id,leg_id,credential,
                           physical_space,at) VALUES(?,?,?,?,?,?)""",
                    (scan_id, passenger_id, leg_id, credential, physical_space, parse_time(at).isoformat()),
                )
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                self.conn.rollback()
                return False

    def get_boarding(self, passenger_id, leg_id) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM boarding_facts WHERE passenger_id=? AND leg_id=?",
                (passenger_id, leg_id),
            ).fetchone()
        return dict(row) if row else None

    def list_boarding(self, leg_id=None) -> list[dict]:
        with self._lock:
            if leg_id:
                rows = self.conn.execute(
                    "SELECT * FROM boarding_facts WHERE leg_id=? ORDER BY at", (leg_id,)
                ).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM boarding_facts ORDER BY at").fetchall()
        return [dict(row) for row in rows]

    def record_decision(self, case_id, passenger_id, decision, split_allowed, at):
        with self._lock:
            self.conn.execute(
                """INSERT INTO passenger_decisions(case_id,passenger_id,decision,
                       split_allowed,at) VALUES(?,?,?,?,?)
                   ON CONFLICT(case_id,passenger_id) DO UPDATE SET
                     decision=excluded.decision, split_allowed=excluded.split_allowed,
                     at=excluded.at""",
                (case_id, passenger_id, decision, int(split_allowed), _now_iso(at)),
            )
            self.conn.commit()

    def list_decisions(self, case_id) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM passenger_decisions WHERE case_id=? ORDER BY at", (case_id,)
            ).fetchall()
        return [
            {
                "case_id": row["case_id"], "passenger_id": row["passenger_id"],
                "decision": row["decision"], "split_allowed": bool(row["split_allowed"]),
                "at": row["at"],
            }
            for row in rows
        ]

    def upsert_external_occupancy(self, leg_id, space_label, state, reason, at):
        with self._lock:
            self.conn.execute(
                """INSERT INTO external_occupancy(leg_id,space_label,state,reason,at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(leg_id,space_label) DO UPDATE SET
                     state=excluded.state, reason=excluded.reason, at=excluded.at""",
                (leg_id, space_label, state, reason, parse_time(at).isoformat()),
            )
            self.conn.commit()

    def list_external_occupancy(self, leg_id=None) -> list[dict]:
        with self._lock:
            if leg_id:
                rows = self.conn.execute(
                    "SELECT * FROM external_occupancy WHERE leg_id=? ORDER BY space_label", (leg_id,)
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM external_occupancy ORDER BY leg_id,space_label"
                ).fetchall()
        return [dict(row) for row in rows]

    # --- 承运方操作 / 幂等 ---
    def upsert_carrier_op(self, op, now):
        with self._lock:
            self.conn.execute(
                """INSERT INTO carrier_ops(op_id,carrier,operation,request_json,state,
                       remote_ref,reply_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(op_id) DO UPDATE SET state=excluded.state,
                     remote_ref=excluded.remote_ref, reply_json=excluded.reply_json,
                     updated_at=excluded.updated_at""",
                (op["op_id"], op["carrier"], op["operation"], json.dumps(op["request"], ensure_ascii=False),
                 op["state"], op.get("remote_ref"),
                 json.dumps(op["reply"], ensure_ascii=False) if op.get("reply") is not None else None,
                 op.get("created_at", _now_iso(now)), _now_iso(now)),
            )
            self.conn.commit()

    def get_carrier_op(self, op_id: str) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM carrier_ops WHERE op_id=?", (op_id,)).fetchone()
        if not row:
            return None
        return {
            "op_id": row["op_id"], "carrier": row["carrier"], "operation": row["operation"],
            "request": json.loads(row["request_json"]), "state": row["state"],
            "remote_ref": row["remote_ref"],
            "reply": json.loads(row["reply_json"]) if row["reply_json"] else None,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def list_carrier_ops(self, state=None) -> list[dict]:
        with self._lock:
            if state:
                rows = self.conn.execute(
                    "SELECT op_id FROM carrier_ops WHERE state=? ORDER BY created_at", (state,)
                ).fetchall()
            else:
                rows = self.conn.execute("SELECT op_id FROM carrier_ops ORDER BY created_at").fetchall()
        return [self.get_carrier_op(row["op_id"]) for row in rows]

    def store_idempotent(self, request_id, response, now):
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO idempotency(request_id,response_json,at) VALUES(?,?,?)",
                (request_id, json.dumps(response, ensure_ascii=False), _now_iso(now)),
            )
            self.conn.commit()

    def get_idempotent(self, request_id: str) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT response_json FROM idempotency WHERE request_id=?", (request_id,)
            ).fetchone()
        return json.loads(row["response_json"]) if row else None


class MemoryRepository(Repository):
    """与 SQLite 实现同语义的内存台账，供单元测试使用。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.legs: dict[str, LegConfig] = {}
        self.cases: dict[str, dict] = {}
        self.members: dict[str, list[Member]] = {}
        self.allocations: dict[str, AllocationView] = {}
        self.boarding: dict[tuple[str, str], dict] = {}
        self.scans: dict[str, dict] = {}
        self.decisions: dict[tuple[str, str], dict] = {}
        self.external: dict[tuple[str, str], dict] = {}
        self.ops: dict[str, dict] = {}
        self.idempotency: dict[str, dict] = {}

    @contextmanager
    def transaction(self):
        with self._lock:
            yield self

    def upsert_leg(self, leg):
        with self._lock:
            self.legs[leg.leg_id] = leg

    def get_leg(self, leg_id):
        return self.legs.get(leg_id)

    def list_legs(self):
        return list(self.legs.values())

    def mark_leg_cancelled(self, leg_id, cancelled):
        with self._lock:
            leg = self.legs[leg_id]
            self.legs[leg_id] = LegConfig(
                leg.leg_id, leg.carrier, leg.step_free, leg.spaces,
                leg.conflict_groups, leg.boarding_cutoff, leg.arrives, cancelled,
            )

    def record_leg_delay(self, leg_id, arrives):
        with self._lock:
            leg = self.legs[leg_id]
            self.legs[leg_id] = LegConfig(
                leg.leg_id, leg.carrier, leg.step_free, leg.spaces,
                leg.conflict_groups, leg.boarding_cutoff, parse_time(arrives), leg.cancelled,
            )

    def create_case(self, case_id, party_id, itinerary, members, split_allowed, now):
        with self._lock:
            if case_id in self.cases:
                raise Conflict(f"case {case_id} exists")
            self.cases[case_id] = {
                "case_id": case_id, "party_id": party_id, "itinerary": list(itinerary),
                "state": CaseState.PENDING, "split_allowed": split_allowed,
                "created_at": _now_iso(now), "updated_at": _now_iso(now), "decision_note": None,
            }
            self.members[case_id] = list(members)

    def update_case_itinerary(self, case_id, itinerary, now, note=None):
        with self._lock:
            case = self.cases[case_id]
            case["itinerary"] = list(itinerary)
            case["updated_at"] = _now_iso(now)
            if note:
                case["decision_note"] = note

    def update_case_state(self, case_id, state, now, note=None):
        with self._lock:
            case = self.cases[case_id]
            case["state"] = state
            case["updated_at"] = _now_iso(now)
            if note:
                case["decision_note"] = note

    def get_case(self, case_id):
        case = self.cases.get(case_id)
        return dict(case) if case else None

    def list_cases(self):
        return [dict(case) for case in self.cases.values()]

    def list_members(self, case_id):
        return list(self.members.get(case_id, []))

    def add_allocation(self, allocation, now):
        with self._lock:
            if allocation.allocation_id in self.allocations:
                raise Conflict(f"allocation {allocation.allocation_id} exists")
            for existing in self.allocations.values():
                if (
                    existing.leg_id == allocation.leg_id
                    and existing.space_label == allocation.space_label
                    and existing.state in OCCUPYING_STATES
                ):
                    raise Conflict(f"space {allocation.leg_id}:{allocation.space_label} already occupied")
            self.allocations[allocation.allocation_id] = allocation

    def update_allocation_state(self, allocation_id, state, now, remote_hold=None, request_op_id=None):
        with self._lock:
            allocation = self.allocations.get(allocation_id)
            if allocation is None:
                raise Conflict(f"unknown allocation {allocation_id}")
            if allocation.state is AllocState.RELEASED and state is not AllocState.RELEASED:
                raise Conflict(f"allocation {allocation_id} already released")
            updated = AllocationView(
                allocation.allocation_id, allocation.case_id, allocation.passenger_id,
                allocation.leg_id, allocation.space_label, allocation.kind, state,
                allocation.credential,
                request_op_id or allocation.request_op_id,
                remote_hold or allocation.remote_hold,
            )
            self.allocations[allocation_id] = updated

    def list_allocations(self, case_id=None):
        with self._lock:
            values = list(self.allocations.values())
        if case_id:
            values = [a for a in values if a.case_id == case_id]
        return values

    def get_allocation(self, allocation_id):
        return self.allocations.get(allocation_id)

    def find_allocation(self, case_id, passenger_id, leg_id):
        for allocation in self.allocations.values():
            if (allocation.case_id, allocation.passenger_id, allocation.leg_id) == (
                case_id, passenger_id, leg_id,
            ):
                return allocation
        return None

    def record_boarding(self, scan_id, passenger_id, leg_id, credential, physical_space, at):
        with self._lock:
            key = (passenger_id, leg_id)
            if key in self.boarding or scan_id in self.scans:
                return False
            fact = {
                "scan_id": scan_id, "passenger_id": passenger_id, "leg_id": leg_id,
                "credential": credential, "physical_space": physical_space,
                "at": parse_time(at).isoformat(),
            }
            self.boarding[key] = fact
            self.scans[scan_id] = fact
            return True

    def get_boarding(self, passenger_id, leg_id):
        fact = self.boarding.get((passenger_id, leg_id))
        return dict(fact) if fact else None

    def list_boarding(self, leg_id=None):
        values = list(self.boarding.values())
        if leg_id:
            values = [fact for fact in values if fact["leg_id"] == leg_id]
        return [dict(fact) for fact in values]

    def record_decision(self, case_id, passenger_id, decision, split_allowed, at):
        with self._lock:
            self.decisions[(case_id, passenger_id)] = {
                "case_id": case_id, "passenger_id": passenger_id,
                "decision": decision, "split_allowed": bool(split_allowed),
                "at": _now_iso(at),
            }

    def list_decisions(self, case_id):
        return [
            dict(row) for row in self.decisions.values() if row["case_id"] == case_id
        ]

    def upsert_external_occupancy(self, leg_id, space_label, state, reason, at):
        with self._lock:
            self.external[(leg_id, space_label)] = {
                "leg_id": leg_id, "space_label": space_label, "state": state,
                "reason": reason, "at": parse_time(at).isoformat(),
            }

    def list_external_occupancy(self, leg_id=None):
        values = list(self.external.values())
        if leg_id:
            values = [row for row in values if row["leg_id"] == leg_id]
        return [dict(row) for row in values]

    def upsert_carrier_op(self, op, now):
        with self._lock:
            existing = self.ops.get(op["op_id"])
            self.ops[op["op_id"]] = {
                "op_id": op["op_id"], "carrier": op["carrier"], "operation": op["operation"],
                "request": op["request"], "state": op["state"],
                "remote_ref": op.get("remote_ref"),
                "reply": op.get("reply"),
                "created_at": (existing or {}).get("created_at", _now_iso(now)),
                "updated_at": _now_iso(now),
            }

    def get_carrier_op(self, op_id):
        op = self.ops.get(op_id)
        return dict(op) if op else None

    def list_carrier_ops(self, state=None):
        values = list(self.ops.values())
        if state:
            values = [op for op in values if op["state"] == state]
        return [dict(op) for op in values]

    def store_idempotent(self, request_id, response, now):
        self.idempotency.setdefault(request_id, response)

    def get_idempotent(self, request_id):
        return self.idempotency.get(request_id)
