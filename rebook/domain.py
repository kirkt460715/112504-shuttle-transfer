"""纯领域规则：空间互斥、需求匹配、换乘判定与全程可兑现性。

本模块不做任何 IO，也不依赖台账实现，所有判断只基于传入的数据结构，
以便针对每条业务规则编写独立单元测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Iterable, Mapping, Optional, Sequence


class Need(str, Enum):
    """旅客对单段车次的乘载需求。"""

    STANDARD = "standard"      # 普通座位
    WHEELCHAIR = "wheelchair"  # 必须使用无台阶车辆的轮椅位


class SpaceKind(str, Enum):
    SEAT = "seat"
    BAY = "bay"  # 轮椅位


class AllocState(str, Enum):
    """单个座位/轮椅位安排的生命周期。"""

    PRE_CLAIMED = "pre_claimed"   # 本地预占，远端结果未明（可能已占用）
    HELD = "held"                 # 承运方确认占座
    BOARDED = "boarded"           # 已凭票/凭证实际登车
    RELEASABLE = "releasable"     # 可释放：被替代方案取代或未登车
    RELEASED = "released"         # 值班台已确认释放，物理空间回到可用池
    FAILED = "failed"             # 远端明确拒绝，预占已作废


# 仍然占据物理空间、不能再分配给别人的状态。
OCCUPYING_STATES = frozenset(
    {AllocState.PRE_CLAIMED, AllocState.HELD, AllocState.BOARDED, AllocState.RELEASABLE}
)
# 已确立、可以向旅客承诺的保障状态。
SECURED_STATES = frozenset({AllocState.HELD, AllocState.BOARDED})


class CaseState(str, Enum):
    """整段改签方案的状态。只有全程可兑现时才允许进入 CONFIRMED。"""

    PENDING = "pending"        # 旅客尚未接受或保障尚未齐备
    CONFIRMED = "confirmed"    # 已发出可兑现的整段改签确认
    PARTIAL = "partial"        # 部分成行（至少一段已登车），后续段待保障
    WAITING_DESK = "waiting_desk"  # 出现断线/晚点等结果未明情况，须值班台处理
    CLOSED = "closed"          # 方案结束（全部登车完成或终止）


@dataclass(frozen=True)
class Space:
    leg_id: str
    label: str
    kind: SpaceKind

    @property
    def key(self) -> str:
        return f"{self.leg_id}:{self.label}"


@dataclass
class LegConfig:
    """一段接驳车的静态配置。"""

    leg_id: str
    carrier: str
    step_free: bool
    spaces: dict[str, Space]                      # label -> Space
    conflict_groups: list[frozenset[str]]         # 物理空间互斥分组（含自身）
    boarding_cutoff: Optional[datetime] = None    # 最晚登车时间
    arrives: Optional[datetime] = None            # （首段）预计/实际到达时间
    cancelled: bool = False

    def group_of(self, label: str) -> frozenset[str]:
        for group in self.conflict_groups:
            if label in group:
                return group
        return frozenset({label})

    def blocked_by(self, label: str) -> frozenset[str]:
        """占用 label 时同时被占据的全部标识。"""
        return self.group_of(label)

    def is_open_for_holds(self, now: Optional[datetime] = None) -> bool:
        """车次是否仍可发起占座。

        已取消、或当前时间已过最晚登车时间的车次关闭占座通道：
        已在车旅客只能凭登车事实归集，未登车者由发车清单按未到处理。
        """
        if self.cancelled:
            return False
        if self.boarding_cutoff is None:
            return True
        moment = parse_time(now) if now else datetime.now(self.boarding_cutoff.tzinfo)
        return moment < parse_time(self.boarding_cutoff)


@dataclass(frozen=True)
class Member:
    passenger_id: str
    need: Need
    credential: str  # 原票或替代凭证，登车事实归集到它


@dataclass(frozen=True)
class AllocationView:
    allocation_id: str
    case_id: str
    passenger_id: str
    leg_id: str
    space_label: str
    kind: SpaceKind
    state: AllocState
    credential: str
    request_op_id: Optional[str] = None
    remote_hold: Optional[str] = None


@dataclass(frozen=True)
class LegPlan:
    """一段车的保障结论。"""

    leg_id: str
    ok: bool
    reason: Optional[str] = None
    assignments: Mapping[str, str] = field(default_factory=dict)  # passenger -> space label


@dataclass(frozen=True)
class FeasibilityReport:
    ok: bool
    legs: tuple[LegPlan, ...]
    reasons: tuple[str, ...]

    @property
    def reason(self) -> Optional[str]:
        return "; ".join(self.reasons) if self.reasons else None


def parse_time(value: str | datetime) -> datetime:
    """解析 ISO-8601 时间；无时区补 UTC，保证比较双方均可感知时区。"""
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        from datetime import timezone

        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def build_conflict_groups(
    labels: Iterable[str], shared: Mapping[str, Iterable[str]] | None = None
) -> list[frozenset[str]]:
    """根据共享关系（轮椅位与折叠座同空间）计算互斥分组的连通分量。

    ``shared`` 的键与值互相可达，例如 ``{"bay-4": ["seat-5", "seat-6"]}``
    表示三者占用的是同一物理空间，任一被占则其余不可再分。
    """
    adjacency: dict[str, set[str]] = {label: set() for label in labels}
    for anchor, neighbors in (shared or {}).items():
        adjacency.setdefault(anchor, set())
        for neighbor in neighbors:
            adjacency.setdefault(neighbor, set())
            adjacency[anchor].add(neighbor)
            adjacency[neighbor].add(anchor)

    seen: set[str] = set()
    groups: list[frozenset[str]] = []
    for label in adjacency:
        if label in seen:
            continue
        stack = [label]
        component: set[str] = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(adjacency[current])
        seen |= component
        groups.append(frozenset(component))
    return groups


def space_matches(space: Space, need: Need, leg: LegConfig) -> bool:
    """座位能否满足旅客需求。轮椅需求只能给轮椅位，且车辆必须无台阶。"""
    if need is Need.WHEELCHAIR:
        return leg.step_free and space.kind is SpaceKind.BAY
    return space.kind is SpaceKind.SEAT


def transfer_feasible(
    first: LegConfig,
    second: LegConfig,
    effective_arrival: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> bool:
    """前车（含晚点）到达是否赶得上后车最晚登车时间。

    截止时间独立于前车到达时间：以后车 ``boarding_cutoff`` 为准。
    缺少到达或截止信息时不做时间否决，交给其余规则判断。
    """
    arrival = effective_arrival or first.arrives
    cutoff = second.boarding_cutoff
    if arrival is None or cutoff is None:
        return True
    arrival = parse_time(arrival)
    cutoff = parse_time(cutoff)
    if now is not None and parse_time(now) >= cutoff:
        return False
    return arrival < cutoff


def occupied_labels(
    leg_id: str, allocations: Iterable[AllocationView]
) -> set[str]:
    """当前仍占用该段物理空间的座位标识（含 RELEASABLE，未确认释放前不可再分）。"""
    occupied: set[str] = set()
    for allocation in allocations:
        if allocation.leg_id == leg_id and allocation.state in OCCUPYING_STATES:
            occupied.add(allocation.space_label)
    return occupied


def available_spaces(
    leg: LegConfig,
    blocked_labels: Iterable[str],
    external_occupied: Iterable[str] = (),
) -> set[str]:
    """扣除互斥分组占用与外部占座后，该段仍可分配的座位标识。"""
    blocked: set[str] = set()
    for label in blocked_labels:
        blocked |= leg.group_of(label)
    for label in external_occupied:
        blocked |= leg.group_of(label)
    return {label for label in leg.spaces if label not in blocked}


def plan_leg(
    leg: LegConfig,
    members: Sequence[Member],
    blocked_labels: Iterable[str],
    external_occupied: Iterable[str] = (),
    fixed: Mapping[str, str] | None = None,
) -> LegPlan:
    """为一段车规划座位。

    - 轮椅需求只能落轮椅位，普通需求只能落普通座；
    - 共享物理空间按互斥分组整体视为不可用；
    - ``fixed`` 给出已存在的安排（已登车/已占座），规划必须沿用而非重占；
    - 轮椅位稀缺，优先回填轮椅需求，再安排普通座位，采用回溯保证可解时不被贪心堵死。
    """
    fixed = fixed or {}
    if leg.cancelled:
        return LegPlan(leg.leg_id, False, "leg_cancelled")

    available = available_spaces(leg, blocked_labels, external_occupied)
    assignments: dict[str, str] = dict(fixed)
    missing: list[str] = []

    pending = [member for member in members if member.passenger_id not in fixed]
    # 轮椅需求优先（资源最受限）。
    pending.sort(key=lambda member: 0 if member.need is Need.WHEELCHAIR else 1)

    def backtrack(index: int) -> bool:
        if index == len(pending):
            return True
        member = pending[index]
        candidates = sorted(
            label
            for label in available
            if label not in assignments.values()
            and space_matches(leg.spaces[label], member.need, leg)
        )
        if not candidates:
            return False
        for label in candidates:
            assignments[member.passenger_id] = label
            if backtrack(index + 1):
                return True
            del assignments[member.passenger_id]
        return False

    if not backtrack(0):
        for member in pending:
            kind = "wheelchair_bay" if member.need is Need.WHEELCHAIR else "seat"
            if not any(
                label not in assignments.values()
                and space_matches(leg.spaces[label], member.need, leg)
                for label in available
            ):
                missing.append(f"{member.passenger_id}:no_{kind}")
        if not missing:
            missing.append("no_matching_assignment")
        return LegPlan(leg.leg_id, False, ",".join(missing), dict(assignments))

    return LegPlan(leg.leg_id, True, assignments=dict(assignments))


def evaluate_itinerary(
    itinerary: Sequence[str],
    members: Sequence[Member],
    legs_by_id: Mapping[str, LegConfig],
    allocations: Sequence[AllocationView],
    external_occupied: Mapping[str, Iterable[str]] | None = None,
    now: Optional[datetime] = None,
    split_allowed: bool = False,
    covered_passengers: Iterable[str] | None = None,
    boarded_pairs: Iterable[tuple[str, str]] | None = None,
) -> FeasibilityReport:
    """判断一个候选联程方案对该同行组是否全程可兑现。

    可兑现的充要条件：

    1. 未拆分时所有成员都在方案覆盖范围内（明确同意拆分才允许子集成行）；
    2. 仍开放占座的车次，每名成员都能按需求分到座位/轮椅位，已登车或已占座
       的安排沿用不重占；已关闭占座通道的车次只认登车事实；
    3. 相邻两段满足换乘截止时间（前车晚点纳入计算）；
    4. 任何一段车次未取消。
    """
    external_occupied = external_occupied or {}
    covered = set(covered_passengers or ())
    boarded = set(boarded_pairs or ())
    reasons: list[str] = []
    plans: list[LegPlan] = []

    active_members = list(members)
    if covered:
        if not split_allowed and covered != {m.passenger_id for m in members}:
            reasons.append("split_requires_consent")
        active_members = [m for m in members if m.passenger_id not in covered]

    member_ids = {m.passenger_id for m in active_members}
    for position, leg_id in enumerate(itinerary):
        leg = legs_by_id.get(leg_id)
        if leg is None:
            plans.append(LegPlan(leg_id, False, "unknown_leg"))
            reasons.append(f"{leg_id}:unknown_leg")
            continue
        if leg.cancelled:
            plans.append(LegPlan(leg_id, False, "leg_cancelled"))
            reasons.append(f"{leg_id}:leg_cancelled")
            continue

        if not leg.is_open_for_holds(now):
            # 占座通道已关闭：保障只能来自登车事实，不能再规划新座位。
            missing_boarding = [
                member.passenger_id
                for member in active_members
                if (member.passenger_id, leg_id) not in boarded
            ]
            if missing_boarding:
                reason = "closed_leg_without_boarding:" + ",".join(missing_boarding)
                plans.append(LegPlan(leg_id, False, reason))
                reasons.append(f"{leg_id}:{reason}")
            else:
                plans.append(LegPlan(leg_id, True, assignments={
                    member.passenger_id: next(
                        (
                            allocation.space_label
                            for allocation in allocations
                            if allocation.leg_id == leg_id
                            and allocation.passenger_id == member.passenger_id
                        ),
                        "boarded",
                    )
                    for member in active_members
                }))
        else:
            leg_allocations = [a for a in allocations if a.leg_id == leg_id]
            blocked = occupied_labels(leg_id, allocations)
            fixed = {
                allocation.passenger_id: allocation.space_label
                for allocation in leg_allocations
                if allocation.state in SECURED_STATES
                and allocation.passenger_id in member_ids
            }
            plan = plan_leg(
                leg,
                active_members,
                blocked,
                external_occupied.get(leg_id, ()),
                fixed=fixed,
            )
            plans.append(plan)
            if not plan.ok:
                reasons.append(f"{leg_id}:{plan.reason}")

        if position > 0:
            previous = legs_by_id[itinerary[position - 1]]
            if not transfer_feasible(previous, leg, now=now):
                reasons.append(
                    f"{previous.leg_id}->{leg_id}:missed_boarding_cutoff"
                )

    ok = not reasons
    return FeasibilityReport(ok, tuple(plans), tuple(reasons))
