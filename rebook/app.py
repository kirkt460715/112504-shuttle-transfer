"""应用服务：编排领域规则、台账与承运方调用。

关键事务约定：

- 发起远端占座前先在本地写入 ``PRE_CLAIMED`` 预占（唯一索引拦截同空间双占）。
- 对端明确拒绝 → 预占作废（``FAILED``），物理空间释放；连接断开结果未明 →
  保留 ``PRE_CLAIMED`` 与 ``unknown`` 操作记录，值班台可凭操作编号补查，
  补查成功转 ``HELD``，确认未接单才允许作废。
- 只有方案内所有成员的全部剩余段都处于已占座/已登车且换乘条件成立时，
  方案才进入 ``CONFIRMED``；否则保持待处理，绝不向旅客发出无法兑现的承诺。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional
from uuid import uuid4

from .carrier import CarrierPort, CarrierRejected, CarrierUnavailable
from .domain import (
    AllocState,
    AllocationView,
    CaseState,
    FeasibilityReport,
    LegConfig,
    Member,
    Need,
    SECURED_STATES,
    SpaceKind,
    evaluate_itinerary,
    parse_time,
)
from .store import Conflict, Repository


class ServiceError(Exception):
    """业务规则错误（请求可修正）。"""


class NotFound(ServiceError):
    """凭证或方案不存在。"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RebookService:
    def __init__(self, repository: Repository, carriers: CarrierPort, clock=utc_now):
        self.repo = repository
        self.carriers = carriers
        self.clock = clock

    # ------------------------------------------------------------------ 基础
    def _leg(self, leg_id: str) -> LegConfig:
        leg = self.repo.get_leg(leg_id)
        if leg is None:
            raise NotFound(f"unknown leg {leg_id}")
        return leg

    def _case(self, case_id: str) -> dict:
        case = self.repo.get_case(case_id)
        if case is None:
            raise NotFound(f"unknown case {case_id}")
        return case

    def _external_occupied(self, leg_id: str) -> list[str]:
        # 只有状态仍为 occupied 的外部记录才占物理空间；
        # 已回报 cancelled/released 的记录保留台账用于审计，但不再阻断分配。
        return [
            row["space_label"]
            for row in self.repo.list_external_occupancy(leg_id)
            if row["state"] == "occupied"
        ]

    def _all_external(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for row in self.repo.list_external_occupancy():
            if row["state"] == "occupied":
                result.setdefault(row["leg_id"], []).append(row["space_label"])
        return result

    # ------------------------------------------------------------- 方案管理
    def open_case(
        self,
        case_id: str,
        itinerary: list[str],
        members: list[Member],
        *,
        party_id: Optional[str] = None,
        split_allowed: bool = False,
        request_id: Optional[str] = None,
    ) -> dict:
        """登记一个改签方案（旅客尚未接受或保障尚未齐备）。"""
        if request_id:
            cached = self.repo.get_idempotent(request_id)
            if cached is not None:
                return cached
        if not members:
            raise ServiceError("case requires at least one member")
        for leg_id in itinerary:
            self._leg(leg_id)
        now = self.clock()
        try:
            self.repo.create_case(case_id, party_id, itinerary, members, split_allowed, now)
        except Conflict as exc:
            raise ServiceError(str(exc)) from exc
        response = self.case_status(case_id)
        if request_id:
            self.repo.store_idempotent(request_id, response, now)
        return response

    def check_feasibility(
        self,
        itinerary: list[str],
        members: list[Member],
        *,
        case_id: Optional[str] = None,
        now: Optional[datetime] = None,
        covered_passengers: Optional[Iterable[str]] = None,
    ) -> FeasibilityReport:
        legs_by_id = {leg.leg_id: leg for leg in self.repo.list_legs()}
        allocations = self.repo.list_allocations(case_id) if case_id else self.repo.list_allocations()
        case = self.repo.get_case(case_id) if case_id else None
        member_ids = {member.passenger_id for member in members}
        boarded_pairs = {
            (fact["passenger_id"], fact["leg_id"])
            for fact in self.repo.list_boarding()
            if fact["passenger_id"] in member_ids
        }
        return evaluate_itinerary(
            itinerary,
            members,
            legs_by_id,
            allocations,
            self._all_external(),
            now=now or self.clock(),
            split_allowed=case["split_allowed"] if case else False,
            covered_passengers=covered_passengers,
            boarded_pairs=boarded_pairs,
        )

    def case_status(self, case_id: str) -> dict:
        case = self._case(case_id)
        members = self.repo.list_members(case_id)
        allocations = self.repo.list_allocations(case_id)
        report = self.check_feasibility(case["itinerary"], members, case_id=case_id)
        decisions = {
            row["passenger_id"]: row["decision"]
            for row in self.repo.list_decisions(case_id)
        }
        return {
            "case_id": case_id,
            "party_id": case["party_id"],
            "state": case["state"].value,
            "itinerary": list(case["itinerary"]),
            "split_allowed": case["split_allowed"],
            "feasible": report.ok,
            "reasons": list(report.reasons),
            "members": [
                {
                    "passenger_id": member.passenger_id,
                    "need": member.need.value,
                    "credential": member.credential,
                    "decision": decisions.get(member.passenger_id),
                }
                for member in members
            ],
            "allocations": [self._allocation_dto(a) for a in allocations],
            "updated_at": case["updated_at"],
            "note": case["decision_note"],
        }

    def list_cases(self) -> list[dict]:
        return [self.case_status(case["case_id"]) for case in self.repo.list_cases()]

    # ------------------------------------------------------------- 占座确认
    def record_passenger_decision(
        self,
        case_id: str,
        passenger_id: str,
        decision: str,
        *,
        split_allowed: Optional[bool] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        """登记旅客选择。

        - ``accept``：接受有保障的整程改签；
        - ``retain``：保留原确认、等待新方案，售票台不得对其落实新占座；
        - ``split``：明确同意拆开同行成员（仅在方案允许拆分时生效）。
        """
        if request_id:
            cached = self.repo.get_idempotent(request_id)
            if cached is not None:
                return cached
        case = self._case(case_id)
        member_ids = {member.passenger_id for member in self.repo.list_members(case_id)}
        if passenger_id not in member_ids:
            raise NotFound(f"passenger {passenger_id} not in case {case_id}")
        if decision not in {"accept", "retain", "split"}:
            raise ServiceError(f"unknown decision {decision}")

        effective_split = bool(split_allowed) if split_allowed is not None else (
            decision == "split"
        )
        if effective_split and not case["split_allowed"] and decision != "split":
            # 方案本身禁止拆分时，旅客无法单独打开拆分开关。
            effective_split = False
        self.repo.record_decision(
            case_id, passenger_id, decision, effective_split, self.clock()
        )
        response = self.case_status(case_id)
        response["recorded_decision"] = decision
        if request_id:
            self.repo.store_idempotent(request_id, response, self.clock())
        return response

    def _retaining_passengers(self, case_id: str) -> set[str]:
        """选择保留原确认、不应被纳入新改签保障的旅客。"""
        return {
            row["passenger_id"]
            for row in self.repo.list_decisions(case_id)
            if row["decision"] == "retain"
        }

    def confirm_case(
        self,
        case_id: str,
        *,
        itinerary: Optional[list[str]] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        """尝试把方案做成可兑现的整段确认。

        全程可兑现才返回 ``confirmed``；断线等结果未明时返回 ``waiting_desk``；
        明确无法满足时返回 ``pending`` 并附原因，已持有的安排不受影响。
        选择保留原确认的旅客不会被纳入新保障，也不会触发新的占座。
        """
        if request_id:
            cached = self.repo.get_idempotent(request_id)
            if cached is not None:
                return cached
        case = self._case(case_id)
        if itinerary is not None and itinerary != case["itinerary"]:
            self.repo.update_case_itinerary(
                case_id, itinerary, self.clock(), note="itinerary_replanned"
            )
            case = self._case(case_id)

        all_members = self.repo.list_members(case_id)
        retaining = self._retaining_passengers(case_id)
        members = [
            member for member in all_members
            if member.passenger_id not in retaining
        ]
        if not members:
            # 全员保留原确认：没有需要落实的新保障，方案保持等待新方案。
            self.repo.update_case_state(
                case_id, CaseState.PENDING, self.clock(), note="all_passengers_retain"
            )
            response = self.case_status(case_id)
            if request_id:
                self.repo.store_idempotent(request_id, response, self.clock())
            return response

        # 先用当前台账做规划性评估，得到每段座位规划；已关闭占座通道的车次
        # 只认登车事实，其缺口在落实后重新评估，此处不直接短路。
        plan_report = self.check_feasibility(case["itinerary"], members, case_id=case_id)
        blocking = [
            plan for plan in plan_report.legs
            if not plan.ok and not str(plan.reason).startswith("closed_leg_without_boarding")
        ]
        if blocking:
            # 开放占座的车次本身就无法满足（无轮椅位/空间互斥/车次取消/错过换乘），
            # 不发起任何对端调用，保持待处理。
            self.repo.update_case_state(case_id, CaseState.PENDING, self.clock())
            response = self.case_status(case_id)
            if request_id:
                self.repo.store_idempotent(request_id, response, self.clock())
            return response

        unknowns = self._secure_assignments(case_id, case["itinerary"], members, plan_report)

        # 重新评估：预占/占座落库后，全部剩余段是否都有保障。
        report = self.check_feasibility(case["itinerary"], members, case_id=case_id)
        closed_gaps = self._unsecured_closed_legs(case["itinerary"], members)
        failed = [
            allocation.allocation_id
            for allocation in self.repo.list_allocations(case_id)
            # 只统计当前行程内的失败安排；旧行程的历史失败不阻断新方案
            if allocation.state is AllocState.FAILED
            and allocation.leg_id in case["itinerary"]
        ]
        now = self.clock()
        if unknowns:
            self.repo.update_case_state(
                case_id, CaseState.WAITING_DESK, now,
                note="carrier_result_unknown:" + ",".join(sorted(unknowns)),
            )
        elif closed_gaps:
            # 已关闭占座的车次仍有人没登车：该段不可承诺，保持待处理。
            self.repo.update_case_state(
                case_id, CaseState.PENDING, now,
                note="closed_leg_without_boarding:" + ",".join(closed_gaps),
            )
        elif failed:
            # 承运方明确拒绝过的占座未解除前，不能发出整段确认。
            self.repo.update_case_state(
                case_id, CaseState.PENDING, now,
                note="carrier_rejected_allocations:" + ",".join(failed),
            )
        elif retaining:
            # 有人保留原确认：接受改签的成员可以成行，但整组方案不能称为已确认。
            self.repo.update_case_state(
                case_id, CaseState.PARTIAL if report.ok else CaseState.PENDING,
                now, note="passengers_retaining:" + ",".join(sorted(retaining)),
            )
        elif report.ok:
            self.repo.update_case_state(case_id, CaseState.CONFIRMED, now)
        else:
            self.repo.update_case_state(case_id, CaseState.PENDING, now)
        response = self.case_status(case_id)
        if request_id:
            self.repo.store_idempotent(request_id, response, now)
        return response

    def _secure_assignments(
        self, case_id: str, itinerary: list[str], members: list[Member],
        report: FeasibilityReport,
    ) -> set[str]:
        """按可行规划逐段逐人落实占座，返回结果未明的操作编号集合。

        已关闭占座通道的车次（过最晚登车时间/取消）不再发起占座，
        其保障只能来自登车事实，由 :meth:`_unsecured_closed_legs` 单独把关。
        """
        unknowns: set[str] = set()
        plan_by_leg = {plan.leg_id: plan for plan in report.legs}
        now = self.clock()

        for leg_id in itinerary:
            leg = self._leg(leg_id)
            if not leg.is_open_for_holds(now):
                continue
            plan = plan_by_leg[leg_id]
            for member in members:
                label = plan.assignments.get(member.passenger_id)
                if label is None:
                    continue
                existing = self.repo.find_allocation(case_id, member.passenger_id, leg_id)
                if existing is not None and existing.state in SECURED_STATES:
                    continue  # 已登车/已占座，沿用不重占
                boarding = self.repo.get_boarding(member.passenger_id, leg_id)
                if boarding is not None:
                    continue  # 登车事实已存在，由登车流程归集，不重复占座

                allocation = existing
                if allocation is None:
                    allocation = AllocationView(
                        allocation_id=f"alloc-{uuid4().hex[:12]}",
                        case_id=case_id,
                        passenger_id=member.passenger_id,
                        leg_id=leg_id,
                        space_label=label,
                        kind=leg.spaces[label].kind,
                        state=AllocState.PRE_CLAIMED,
                        credential=member.credential,
                    )
                    try:
                        self.repo.add_allocation(allocation, now)
                    except Conflict as exc:
                        # 同物理空间仍被占用（含结果未明的预占），不能发给另一位旅客。
                        raise ServiceError(f"space unavailable: {exc}") from exc
                elif allocation.state is AllocState.FAILED:
                    # 上一轮被明确拒绝的安排可在重新确认时重试；
                    # 结果未明（PRE_CLAIMED）的安排绝不重试，避免同一空间双占。
                    if allocation.space_label != label:
                        self.repo.update_allocation_state(
                            allocation.allocation_id, AllocState.RELEASED, now
                        )
                        allocation = AllocationView(
                            allocation_id=f"alloc-{uuid4().hex[:12]}",
                            case_id=case_id,
                            passenger_id=member.passenger_id,
                            leg_id=leg_id,
                            space_label=label,
                            kind=leg.spaces[label].kind,
                            state=AllocState.PRE_CLAIMED,
                            credential=member.credential,
                        )
                        self.repo.add_allocation(allocation, now)
                    else:
                        self.repo.update_allocation_state(
                            allocation.allocation_id, AllocState.PRE_CLAIMED, now
                        )
                        allocation = self.repo.get_allocation(allocation.allocation_id)

                if allocation.state is AllocState.PRE_CLAIMED:
                    self._call_hold(case_id, leg, member, label, allocation, unknowns)
        return unknowns

    def _unsecured_closed_legs(
        self, itinerary: list[str], members: list[Member]
    ) -> list[str]:
        """已关闭占座通道但仍有成员没有登车事实的车次，不能承诺可乘。"""
        now = self.clock()
        problems: list[str] = []
        for leg_id in itinerary:
            leg = self._leg(leg_id)
            if leg.is_open_for_holds(now):
                continue
            for member in members:
                fact = self.repo.get_boarding(member.passenger_id, leg_id)
                if fact is None:
                    problems.append(f"{leg_id}:{member.passenger_id}:not_boarded")
        return problems

    def _call_hold(self, case_id, leg: LegConfig, member: Member, label: str,
                   allocation: AllocationView, unknowns: set[str]) -> None:
        op_id = allocation.request_op_id or f"hold-{uuid4().hex[:12]}"
        request = {
            "carrier": leg.carrier, "leg": leg.leg_id,
            "space": label, "passenger": member.passenger_id,
        }
        self.repo.upsert_carrier_op(
            {
                "op_id": op_id, "carrier": leg.carrier, "operation": "hold",
                "request": request, "state": "unknown",
            },
            self.clock(),
        )
        try:
            reply = self.carriers.hold(
                op_id, leg.carrier, leg.leg_id, label, member.passenger_id
            )
        except CarrierRejected as exc:
            self.repo.upsert_carrier_op(
                {"op_id": op_id, "carrier": leg.carrier, "operation": "hold",
                 "request": request, "state": "rejected", "reply": {"error": str(exc)}},
                self.clock(),
            )
            self.repo.update_allocation_state(
                allocation.allocation_id, AllocState.FAILED, self.clock(),
                request_op_id=op_id,
            )
            return
        except CarrierUnavailable as exc:
            # 对端可能已占座成功后断线：保留预占，等待补查，物理空间继续锁定。
            self.repo.upsert_carrier_op(
                {"op_id": op_id, "carrier": leg.carrier, "operation": "hold",
                 "request": request, "state": "unknown", "reply": {"error": str(exc)}},
                self.clock(),
            )
            self.repo.update_allocation_state(
                allocation.allocation_id, AllocState.PRE_CLAIMED, self.clock(),
                request_op_id=op_id,
            )
            unknowns.add(op_id)
            return

        remote_ref = reply.get("remote_hold")
        self.repo.upsert_carrier_op(
            {"op_id": op_id, "carrier": leg.carrier, "operation": "hold",
             "request": request, "state": "held", "remote_ref": remote_ref, "reply": reply},
            self.clock(),
        )
        self.repo.update_allocation_state(
            allocation.allocation_id, AllocState.HELD, self.clock(),
            remote_hold=remote_ref, request_op_id=op_id,
        )

    def reconcile_operation(self, op_id: str) -> dict:
        """值班台补查结果未明的承运方操作。

        对端协议保证同一操作编号重试返回原回报。补查顺序：

        1. ``GET /operations/<id>`` 探查对端日志；
        2. 查不到则用相同编号与请求体重投（断线时对端通常已落库，
           重投会返回原始回报，不会产生第二个占座）；
        3. 确认对端未接单（重投被明确拒绝）→ 预占作废，物理空间才可重新分配；
           补查本身仍失败则继续保持结果未明，锁定不解除。
        """
        op = self.repo.get_carrier_op(op_id)
        if op is None:
            raise NotFound(f"unknown operation {op_id}")
        if op["state"] != "unknown":
            return op
        reply = None
        try:
            reply = self.carriers.query_operation(op_id)
        except CarrierUnavailable:
            reply = None
        if reply is None:
            try:
                reply = self._redeliver(op_id)
            except CarrierRejected:
                # 对端明确没有该操作：预占已在 _redeliver 中作废
                return self.repo.get_carrier_op(op_id)
            if reply is None:
                returned = self.repo.get_carrier_op(op_id)
                returned["state"] = "unknown"
                return returned

        if op["operation"] == "hold":
            remote_ref = reply.get("remote_hold")
            self.repo.upsert_carrier_op(
                {**op, "state": "held", "remote_ref": remote_ref, "reply": reply}, self.clock()
            )
            affected_cases = set()
            for allocation in self.repo.list_allocations():
                if allocation.request_op_id == op_id and allocation.state is AllocState.PRE_CLAIMED:
                    self.repo.update_allocation_state(
                        allocation.allocation_id, AllocState.HELD, self.clock(),
                        remote_hold=remote_ref,
                    )
                    affected_cases.add(allocation.case_id)
            for case_id in affected_cases:
                # 补查落定后重新走确认判定，unknown 全部消除即可发出整段确认。
                self.confirm_case(case_id)
        else:
            self.repo.upsert_carrier_op({**op, "state": "boarded", "reply": reply}, self.clock())
        return self.repo.get_carrier_op(op_id)

    def _redeliver(self, op_id: str) -> Optional[dict]:
        """用原操作编号重投。

        - 对端明确没有接单（拒绝）：作废预占并向上抛出 ``CarrierRejected``；
        - 对端仍不可达（断线等）：返回 None，调用方保持结果未明、锁定不解除。
        """
        op = self.repo.get_carrier_op(op_id)
        request = op["request"]
        try:
            if op["operation"] == "hold":
                return self.carriers.hold(
                    op["op_id"], request["carrier"], request["leg"],
                    request["space"], request["passenger"],
                )
            return self.carriers.fetch_boarding(
                op["op_id"], request["carrier"], request["leg"], request["scan"]
            )
        except CarrierUnavailable:
            return None
        except CarrierRejected as exc:
            self.repo.upsert_carrier_op(
                {**op, "state": "not_accepted", "reply": {"error": str(exc)}}, self.clock()
            )
            self._fail_pre_claims_for(op_id)
            raise

    def _fail_pre_claims_for(self, op_id: str) -> None:
        for allocation in self.repo.list_allocations():
            if allocation.request_op_id == op_id and allocation.state is AllocState.PRE_CLAIMED:
                self.repo.update_allocation_state(
                    allocation.allocation_id, AllocState.FAILED, self.clock()
                )

    # ------------------------------------------------------------- 晚点重判
    def report_delay(self, leg_id: str, arrives: str | datetime) -> dict:
        """前车晚点：更新到达时间并重新判断所有尚未兑现的联程。

        已经登上第一段车的旅客不会被重新占第一段座位；第二段在替代保障落地前，
        原有安排保持有效，不会被悄悄释放。
        """
        moment = parse_time(arrives)
        self.repo.record_leg_delay(leg_id, moment)
        affected: list[dict] = []
        for case in self.repo.list_cases():
            if case["state"] is CaseState.CLOSED:
                continue
            if leg_id not in case["itinerary"]:
                continue
            members = self.repo.list_members(case["case_id"])
            report = self.check_feasibility(
                case["itinerary"], members, case_id=case["case_id"], now=moment
            )
            note = None
            if not report.ok:
                note = "delay_review_required:" + ";".join(report.reasons)
                if case["state"] is CaseState.CONFIRMED:
                    self.repo.update_case_state(
                        case["case_id"], CaseState.WAITING_DESK, self.clock(), note=note
                    )
            affected.append(self.case_status(case["case_id"]))
        return {"leg_id": leg_id, "arrives": moment.isoformat(), "cases": affected}

    def replan_case(
        self,
        case_id: str,
        new_itinerary: list[str],
        *,
        request_id: Optional[str] = None,
    ) -> dict:
        """把方案改到新联程（如 A1+B2）。

        已登车段必须仍是新方案前缀（不会重占已乘段）；新方案保障齐备前，
        旧安排只标记为可释放而不删除，替代失败时仍有有效安排兜底。
        """
        if request_id:
            cached = self.repo.get_idempotent(request_id)
            if cached is not None:
                return cached
        case = self._case(case_id)
        members = self.repo.list_members(case_id)
        for leg_id in new_itinerary:
            self._leg(leg_id)

        boarded_legs = {
            fact["leg_id"]
            for fact in self.repo.list_boarding()
            if fact["passenger_id"] in {m.passenger_id for m in members}
        }
        for old_leg in case["itinerary"]:
            if old_leg in boarded_legs and old_leg not in new_itinerary:
                raise ServiceError(
                    f"cannot drop boarded leg {old_leg}; passenger already departed"
                )

        report = self.check_feasibility(new_itinerary, members, case_id=case_id)
        if not report.ok:
            response = self.case_status(case_id)
            response["replan_rejected"] = list(report.reasons)
            if request_id:
                self.repo.store_idempotent(request_id, response, self.clock())
            return response

        # 新方案可兑现：旧联程中不属于新方案的安排转入可释放（不立即删除）。
        for allocation in self.repo.list_allocations(case_id):
            if (
                allocation.leg_id not in new_itinerary
                and allocation.state in {AllocState.HELD, AllocState.PRE_CLAIMED}
            ):
                self.repo.update_allocation_state(
                    allocation.allocation_id, AllocState.RELEASABLE, self.clock()
                )

        self.repo.update_case_itinerary(
            case_id, new_itinerary, self.clock(), note="replanned"
        )
        response = self.confirm_case(case_id)
        if request_id:
            self.repo.store_idempotent(request_id, response, self.clock())
        return response

    # ------------------------------------------------------------- 登车事实
    def ingest_boarding(
        self,
        scan_id: str,
        carrier: str,
        leg_id: str,
        *,
        credential: Optional[str] = None,
        op_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        """登记迟来的登车事实并归集到原票或替代凭证。

        优先向承运方核对扫码回报；scan 幂等，同一 (旅客, 车次) 不会二次确认座位。
        已经登车的安排不会再发起占座。
        """
        if request_id:
            cached = self.repo.get_idempotent(request_id)
            if cached is not None:
                return cached
        leg = self._leg(leg_id)
        if leg.carrier != carrier:
            raise ServiceError(f"carrier {carrier} does not operate leg {leg_id}")
        op_id = op_id or f"board-{uuid4().hex[:12]}"

        existing = next(
            (fact for fact in self.repo.list_boarding(leg_id) if fact["scan_id"] == scan_id),
            None,
        )
        if existing is not None:
            return {"scan_id": scan_id, "duplicate": True, "fact": existing}

        request = {"carrier": carrier, "leg": leg_id, "scan": scan_id}
        self.repo.upsert_carrier_op(
            {"op_id": op_id, "carrier": carrier, "operation": "boarding",
             "request": request, "state": "unknown"},
            self.clock(),
        )
        try:
            reply = self.carriers.fetch_boarding(op_id, carrier, leg_id, scan_id)
        except CarrierUnavailable:
            # 结果未明同样保留，等待补查，不把已登车旅客当成未出发。
            return {"scan_id": scan_id, "state": "waiting_desk", "op_id": op_id}
        except CarrierRejected as exc:
            self.repo.upsert_carrier_op(
                {"op_id": op_id, "carrier": carrier, "operation": "boarding",
                 "request": request, "state": "rejected", "reply": {"error": str(exc)}},
                self.clock(),
            )
            raise ServiceError(f"boarding scan rejected: {exc}") from exc

        self.repo.upsert_carrier_op(
            {"op_id": op_id, "carrier": carrier, "operation": "boarding",
             "request": request, "state": "boarded", "reply": reply},
            self.clock(),
        )
        passenger_id = reply["passenger"]
        physical_space = reply.get("physical_space")
        matched_credential = self._match_credential(passenger_id, leg_id, credential)
        stored = self.repo.record_boarding(
            scan_id, passenger_id, leg_id, matched_credential,
            physical_space, self.clock(),
        )
        if not stored:
            fact = self.repo.get_boarding(passenger_id, leg_id)
            return {"scan_id": scan_id, "duplicate": True, "fact": fact}

        self._apply_boarding_to_case(passenger_id, leg_id, physical_space, matched_credential)
        response = {
            "scan_id": scan_id,
            "state": "boarded",
            "fact": self.repo.get_boarding(passenger_id, leg_id),
        }
        if request_id:
            self.repo.store_idempotent(request_id, response, self.clock())
        return response

    def _match_credential(self, passenger_id: str, leg_id: str,
                          supplied: Optional[str]) -> str:
        """登车凭证优先取方案内该旅客该段的替代凭证，其次采用回报/请求携带值。"""
        for case in self.repo.list_cases():
            members = {m.passenger_id: m for m in self.repo.list_members(case["case_id"])}
            if passenger_id not in members:
                continue
            allocation = self.repo.find_allocation(
                case["case_id"], passenger_id, leg_id
            )
            if allocation is not None:
                return allocation.credential
            return members[passenger_id].credential
        if supplied:
            return supplied
        raise ServiceError(f"cannot attribute boarding for passenger {passenger_id}")

    def _apply_boarding_to_case(self, passenger_id, leg_id, physical_space, credential):
        for case in self.repo.list_cases():
            case_id = case["case_id"]
            member_ids = {m.passenger_id for m in self.repo.list_members(case_id)}
            if passenger_id not in member_ids or leg_id not in case["itinerary"]:
                continue
            allocation = self.repo.find_allocation(case_id, passenger_id, leg_id)
            if allocation is not None:
                if allocation.state in {AllocState.HELD, AllocState.PRE_CLAIMED,
                                        AllocState.RELEASABLE}:
                    self.repo.update_allocation_state(
                        allocation.allocation_id, AllocState.BOARDED, self.clock()
                    )
            else:
                leg = self._leg(leg_id)
                space = leg.spaces.get(physical_space) if physical_space else None
                self.repo.add_allocation(
                    AllocationView(
                        allocation_id=f"alloc-{uuid4().hex[:12]}",
                        case_id=case_id,
                        passenger_id=passenger_id,
                        leg_id=leg_id,
                        space_label=physical_space or "manifest-only",
                        kind=space.kind if space else SpaceKind.SEAT,
                        state=AllocState.BOARDED,
                        credential=credential,
                    ),
                    self.clock(),
                )
            self._refresh_case_progress(case_id)

    def _refresh_case_progress(self, case_id: str) -> None:
        case = self._case(case_id)
        if case["state"] is CaseState.CLOSED:
            return
        members = self.repo.list_members(case_id)
        allocations = self.repo.list_allocations(case_id)
        member_ids = {m.passenger_id for m in members}
        boarded_pairs = {
            (fact["passenger_id"], fact["leg_id"])
            for fact in self.repo.list_boarding()
            if fact["passenger_id"] in member_ids
        }
        required = {
            (member.passenger_id, leg_id)
            for member in members for leg_id in case["itinerary"]
        }
        if boarded_pairs >= required:
            self.repo.update_case_state(case_id, CaseState.CLOSED, self.clock(),
                                        note="all_legs_boarded")
            return
        if boarded_pairs:
            self.repo.update_case_state(case_id, CaseState.PARTIAL, self.clock())

    # ------------------------------------------------------------- 占座变动
    def record_space_event(
        self, leg_id: str, space_label: str, reason: str, at: str | datetime,
        *,
        request_id: Optional[str] = None,
    ) -> dict:
        """接收承运方/外部座席占用回报（如另一旅客取消释放座位）。

        释放事件只在确认该物理空间当前无占用安排时才腾出；腾出后自动重估
        正在等待的同行组，只有全程重新可兑现才会把改签称为已确认。
        """
        if request_id:
            cached = self.repo.get_idempotent(request_id)
            if cached is not None:
                return cached
        self._leg(leg_id)
        moment = parse_time(at)
        state = "released" if reason == "cancelled" else "occupied"
        self.repo.upsert_external_occupancy(leg_id, space_label, state, reason, moment)

        rechecked: list[dict] = []
        if state == "released":
            for case in self.repo.list_cases():
                # 已确认/已关闭的方案无需重估；待处理、部分成行、结果未明都要重新判断。
                if case["state"] in {CaseState.CONFIRMED, CaseState.CLOSED}:
                    continue
                members = self.repo.list_members(case["case_id"])
                report = self.check_feasibility(
                    case["itinerary"], members, case_id=case["case_id"]
                )
                if report.ok:
                    refreshed = self.confirm_case(case["case_id"])
                    # 只有真正兑现为整段确认的方案才计入重检结果；
                    # 仍待承运方应答或结果未明的方案保持原状态。
                    if refreshed["state"] == CaseState.CONFIRMED.value:
                        rechecked.append(refreshed)
        response = {
            "leg_id": leg_id,
            "space_label": space_label,
            "state": state,
            "rechecked_cases": [item["case_id"] for item in rechecked],
        }
        if request_id:
            self.repo.store_idempotent(request_id, response, moment)
        return response

    # ------------------------------------------------------------- 承运清单
    def carrier_manifest(self, carrier: str, leg_id: str) -> dict:
        """承运方发车清单：应留座、已上车、可以释放三个名单。

        清单是只读视图，重复拉取不改变座位状态；释放必须由值班台确认后才生效，
        避免同一座位被二次确认给别人。
        """
        leg = self._leg(leg_id)
        if leg.carrier != carrier:
            raise ServiceError(f"carrier {carrier} does not operate leg {leg_id}")

        reserved: list[dict] = []
        boarded: list[dict] = []
        releasable: list[dict] = []
        for allocation in self.repo.list_allocations():
            if allocation.leg_id != leg_id:
                continue
            dto = self._allocation_dto(allocation)
            if allocation.state is AllocState.BOARDED:
                boarded.append(dto)
            elif allocation.state in {AllocState.HELD, AllocState.PRE_CLAIMED}:
                reserved.append(dto)
            elif allocation.state is AllocState.RELEASABLE:
                releasable.append(dto)

        facts = [
            fact for fact in self.repo.list_boarding(leg_id)
            if not any(item["passenger_id"] == fact["passenger_id"] for item in boarded)
        ]
        return {
            "carrier": carrier,
            "leg_id": leg_id,
            "boarding_cutoff": leg.boarding_cutoff.isoformat() if leg.boarding_cutoff else None,
            "reserved": reserved,
            "boarded": boarded,
            "releasable": releasable,
            "boarding_facts": facts,
        }

    def mark_no_show(self, carrier: str, leg_id: str, allocation_id: str) -> dict:
        """发车时确认某留座旅客未登车：座位转入可释放名单。"""
        leg = self._leg(leg_id)
        if leg.carrier != carrier:
            raise ServiceError(f"carrier {carrier} does not operate leg {leg_id}")
        allocation = self.repo.get_allocation(allocation_id)
        if allocation is None or allocation.leg_id != leg_id:
            raise NotFound(f"unknown allocation {allocation_id}")
        if allocation.state not in {AllocState.HELD, AllocState.PRE_CLAIMED}:
            raise ServiceError(
                f"allocation {allocation_id} in state {allocation.state.value}, cannot mark no-show"
            )
        self.repo.update_allocation_state(
            allocation_id, AllocState.RELEASABLE, self.clock()
        )
        for case in self.repo.list_cases():
            if case["case_id"] == allocation.case_id:
                self._refresh_case_progress(case["case_id"])
        return self._allocation_dto(self.repo.get_allocation(allocation_id))

    def acknowledge_release(self, carrier: str, allocation_id: str) -> dict:
        """值班台确认释放：可释放安排进入终态 RELEASED，物理空间回到可用池。"""
        allocation = self.repo.get_allocation(allocation_id)
        if allocation is None:
            raise NotFound(f"unknown allocation {allocation_id}")
        leg = self._leg(allocation.leg_id)
        if leg.carrier != carrier:
            raise ServiceError(f"carrier {carrier} does not operate leg {allocation.leg_id}")
        if allocation.state is not AllocState.RELEASABLE:
            raise ServiceError(
                f"allocation {allocation_id} is not releasable "
                f"(state={allocation.state.value})"
            )
        self.repo.update_allocation_state(
            allocation_id, AllocState.RELEASED, self.clock()
        )
        return self._allocation_dto(self.repo.get_allocation(allocation_id))

    # ------------------------------------------------------------- 旅客视图
    def passenger_query(self, credential: str) -> dict:
        """旅客按自己的凭证查询每一段能否乘坐、是否必须等待售票台处理。"""
        matches = [
            case for case in self.repo.list_cases()
            if any(member.credential == credential for member in self.repo.list_members(case["case_id"]))
        ]
        if not matches:
            raise NotFound(f"no itinerary for credential {credential}")
        case = matches[0]
        case_id = case["case_id"]
        member = next(
            member for member in self.repo.list_members(case_id)
            if member.credential == credential
        )
        legs_view: list[dict] = []
        must_wait = case["state"] in {CaseState.WAITING_DESK, CaseState.PENDING}
        for leg_id in case["itinerary"]:
            leg = self._leg(leg_id)
            allocation = self.repo.find_allocation(case_id, member.passenger_id, leg_id)
            fact = self.repo.get_boarding(member.passenger_id, leg_id)
            if fact is not None:
                leg_state = "boarded"
                voucher = fact["scan_id"]
            elif allocation is not None and allocation.state in SECURED_STATES:
                leg_state = "bookable"
                voucher = allocation.credential
            elif allocation is not None and allocation.state is AllocState.PRE_CLAIMED:
                leg_state = "waiting_desk"
                voucher = None
            else:
                leg_state = "unsecured"
                voucher = None
            legs_view.append({
                "leg_id": leg_id,
                "carrier": leg.carrier,
                "state": leg_state,
                "voucher": voucher,
                "boarding_cutoff": leg.boarding_cutoff.isoformat() if leg.boarding_cutoff else None,
                "step_free_required": member.need is Need.WHEELCHAIR,
            })
        return {
            "credential": credential,
            "passenger_id": member.passenger_id,
            "case_id": case_id,
            "case_state": case["state"].value,
            "must_wait_for_desk": must_wait and not all(
                leg["state"] == "boarded" for leg in legs_view
            ),
            "legs": legs_view,
        }

    # ------------------------------------------------------------- 值班恢复
    def desk_recovery(self) -> dict:
        """值班台恢复视图：部分成行的方案、结果未明操作、未释放座席。"""
        partial_cases = [
            self.case_status(case["case_id"])
            for case in self.repo.list_cases()
            if case["state"] in {CaseState.PARTIAL, CaseState.WAITING_DESK, CaseState.PENDING}
        ]
        unknown_ops = self.repo.list_carrier_ops("unknown")
        unreleased = [
            self._allocation_dto(allocation)
            for allocation in self.repo.list_allocations()
            if allocation.state in {AllocState.RELEASABLE, AllocState.PRE_CLAIMED}
        ]
        return {
            "partial_or_open_cases": partial_cases,
            "unknown_operations": unknown_ops,
            "unreleased_spaces": unreleased,
        }

    # ------------------------------------------------------------------ DTO
    @staticmethod
    def _allocation_dto(allocation: AllocationView) -> dict:
        return {
            "allocation_id": allocation.allocation_id,
            "case_id": allocation.case_id,
            "passenger_id": allocation.passenger_id,
            "leg_id": allocation.leg_id,
            "space_label": allocation.space_label,
            "kind": allocation.kind.value,
            "state": allocation.state.value,
            "credential": allocation.credential,
            "request_op_id": allocation.request_op_id,
            "remote_hold": allocation.remote_hold,
        }
