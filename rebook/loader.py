"""从 ``fixtures/registers.json`` 引导业务台账。

加载是幂等的：重复执行只更新车次配置与外部占座，已存在的方案与安排不会被重置，
便于服务重启后继续演练。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .domain import (
    LegConfig,
    Member,
    Need,
    Space,
    SpaceKind,
    build_conflict_groups,
    parse_time,
)
from .store import Repository


def _kind_of(label: str) -> SpaceKind:
    return SpaceKind.BAY if label.startswith("bay") else SpaceKind.SEAT


def load_registers(repo: Repository, registers: dict[str, Any]) -> dict[str, Any]:
    """把演练台账写入仓库，返回引导摘要。"""
    carrier_by_leg: dict[str, str] = registers.get("carrier_by_leg", {})
    legs_by_id: dict[str, LegConfig] = {}

    for raw_leg in registers.get("legs", []):
        leg_id = raw_leg["id"]
        labels = list(raw_leg.get("physical_space", []))
        # 首段可能已发车，台账不登记可售空间，只在登车回报里出现实际位号；
        # 从 boarding 事实补录隐含空间，使登车归集有对应物理位。
        # 台账里 boarding 既可能是单个对象，也可能是对象列表。
        boarding = registers.get("boarding", [])
        boarding_facts = boarding if isinstance(boarding, list) else [boarding]
        for fact in boarding_facts:
            if fact.get("leg") == leg_id:
                # 位号可能只在承运方登车回报里出现；台账缺省时按旅客占一个默认位。
                labels.append(fact.get("physical_space") or f"bay-{fact.get('passenger', 'unknown')}")
        labels = list(dict.fromkeys(labels))
        shared = raw_leg.get("shared", {})
        groups = build_conflict_groups(labels, shared)
        has_bay = any(_kind_of(label) is SpaceKind.BAY for label in labels)
        # 有轮椅位的接驳车即视为无台阶车辆；无轮椅位则不具备轮椅乘载条件。
        step_free = has_bay
        spaces = {
            label: Space(leg_id, label, _kind_of(label)) for label in labels
        }
        cutoff = raw_leg.get("boarding_cutoff")
        arrives = raw_leg.get("arrives")
        leg = LegConfig(
            leg_id=leg_id,
            carrier=carrier_by_leg.get(leg_id, "unknown"),
            step_free=step_free,
            spaces=spaces,
            conflict_groups=groups,
            boarding_cutoff=parse_time(cutoff) if cutoff else None,
            arrives=parse_time(arrives) if arrives else None,
        )
        if "delay_min" in raw_leg and leg.arrives is not None:
            from datetime import timedelta

            leg = LegConfig(
                leg.leg_id, leg.carrier, leg.step_free, leg.spaces,
                leg.conflict_groups, leg.boarding_cutoff,
                leg.arrives + timedelta(minutes=raw_leg["delay_min"]),
                leg.cancelled,
            )
        legs_by_id[leg_id] = leg
        repo.upsert_leg(leg)

    # 台账里已有的外部占座（如 B1:seat-5 被占）。
    for entry in registers.get("filled", []):
        leg_id, label = entry.split(":", 1)
        repo.upsert_external_occupancy(
            leg_id, label, "occupied", "register", parse_time(registers.get("loaded_at", "2028-07-03T09:55:00+08:00"))
        )
    for event in registers.get("capacity_events", []):
        state = "released" if event.get("reason") == "cancelled" else "occupied"
        repo.upsert_external_occupancy(
            event["leg"], event["space"], state, event.get("reason"),
            parse_time(event["at"]),
        )

    summary = {
        "legs": list(legs_by_id),
        "external_occupancy": repo.list_external_occupancy(),
        "party": registers.get("party"),
        "candidate_itineraries": registers.get("candidate_itineraries", {}),
    }

    party = registers.get("party")
    if party and not any(
        case.get("party_id") == party["id"] for case in repo.list_cases()
    ):
        members = _build_members(registers, party)
        primary = next(
            iter(registers.get("candidate_itineraries", {}).values()), []
        )
        if primary:
            from .app import utc_now

            case_id = f"case-{party['id']}"
            repo.create_case(
                case_id=case_id,
                party_id=party["id"],
                itinerary=list(primary),
                members=members,
                split_allowed=bool(party.get("may_split", False)),
                now=utc_now(),
            )
            # 台账里的同行组同意记录：明确同意拆分时为每个成员落一条 split 决定。
            for consent in registers.get("party_consent", []):
                if consent.get("party") != party["id"]:
                    continue
                moment = parse_time(consent["at"])
                for member in members:
                    repo.record_decision(
                        case_id, member.passenger_id,
                        "split" if consent.get("split_allowed") else "accept",
                        bool(consent.get("split_allowed")), moment,
                    )
            summary["opened_case"] = case_id

    return summary


def _build_members(registers: dict[str, Any], party: dict[str, Any]) -> list[Member]:
    original_ticket = registers.get("original", {}).get("ticket", "ticket")
    wheelchair = set(party.get("wheelchair", []))
    escort = party.get("escort")
    members: list[Member] = []
    for passenger_id in registers.get("passengers", []):
        members.append(
            Member(
                passenger_id=passenger_id,
                need=Need.WHEELCHAIR if passenger_id in wheelchair else Need.STANDARD,
                credential=f"{original_ticket}:{passenger_id}",
            )
        )
    if escort and escort not in wheelchair and not any(
        member.passenger_id == escort for member in members
    ):
        members.append(Member(escort, Need.STANDARD, f"{original_ticket}:{escort}"))
    return members


def load_registers_file(repo: Repository, path: str | Path) -> dict[str, Any]:
    registers = json.loads(Path(path).read_text(encoding="utf-8"))
    return load_registers(repo, registers)
