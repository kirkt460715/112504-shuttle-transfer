"""领域规则单元测试：空间互斥、需求匹配、换乘判定与可兑现性。"""
import unittest
from datetime import timedelta

from rebook.domain import (
    AllocState,
    AllocationView,
    LegConfig,
    Member,
    Need,
    Space,
    SpaceKind,
    build_conflict_groups,
    evaluate_itinerary,
    parse_time,
    plan_leg,
    space_matches,
    transfer_feasible,
)

T0 = "2028-07-03T10:00:00+08:00"


def make_leg(leg_id, labels, *, shared=None, cutoff=None, arrives=None,
             step_free=True, carrier="c"):
    spaces = {
        label: Space(leg_id, label, SpaceKind.BAY if label.startswith("bay") else SpaceKind.SEAT)
        for label in labels
    }
    return LegConfig(
        leg_id=leg_id, carrier=carrier, step_free=step_free, spaces=spaces,
        conflict_groups=build_conflict_groups(labels, shared),
        boarding_cutoff=parse_time(cutoff) if cutoff else None,
        arrives=parse_time(arrives) if arrives else None,
    )


class ConflictGroupTest(unittest.TestCase):
    def test_shared_bay_and_foldable_seats_form_one_group(self):
        groups = build_conflict_groups(
            ["bay-4", "seat-5", "seat-6", "seat-8"],
            {"bay-4": ["seat-5", "seat-6"]},
        )
        self.assertIn(frozenset({"bay-4", "seat-5", "seat-6"}), groups)
        self.assertIn(frozenset({"seat-8"}), groups)

    def test_leg_blocks_entire_physical_group(self):
        leg = make_leg("B1", ["bay-4", "seat-5", "seat-6", "seat-8"],
                       shared={"bay-4": ["seat-5", "seat-6"]})
        self.assertEqual(leg.blocked_by("seat-5"), frozenset({"bay-4", "seat-5", "seat-6"}))


class MatchingTest(unittest.TestCase):
    def setUp(self):
        self.leg = make_leg("B1", ["bay-4", "seat-8"])

    def test_wheelchair_need_requires_bay_on_step_free_vehicle(self):
        bay = self.leg.spaces["bay-4"]
        seat = self.leg.spaces["seat-8"]
        self.assertTrue(space_matches(bay, Need.WHEELCHAIR, self.leg))
        # 不能把轮椅旅客降成普通座位
        self.assertFalse(space_matches(seat, Need.WHEELCHAIR, self.leg))
        self.assertTrue(space_matches(seat, Need.STANDARD, self.leg))
        self.assertFalse(space_matches(bay, Need.STANDARD, self.leg))

    def test_wheelchair_need_rejected_without_step_free_vehicle(self):
        leg = LegConfig(
            "X", "c", step_free=False, spaces=self.leg.spaces,
            conflict_groups=self.leg.conflict_groups,
        )
        self.assertFalse(space_matches(leg.spaces["bay-4"], Need.WHEELCHAIR, leg))


class TransferTest(unittest.TestCase):
    def test_delay_can_miss_cutoff(self):
        first = make_leg("A1", ["seat-1"], arrives="2028-07-03T10:35:00+08:00")
        second = make_leg("B1", ["seat-2"], cutoff="2028-07-03T10:42:00+08:00")
        self.assertTrue(transfer_feasible(first, second))
        delayed = LegConfig(
            first.leg_id, first.carrier, first.step_free, first.spaces,
            first.conflict_groups, first.boarding_cutoff,
            first.arrives + timedelta(minutes=14), first.cancelled,
        )
        self.assertFalse(transfer_feasible(delayed, second))

    def test_cutoff_independent_of_arrival_and_now_gate(self):
        first = make_leg("A1", ["seat-1"], arrives="2028-07-03T09:00:00+08:00")
        second = make_leg("B1", ["seat-2"], cutoff="2028-07-03T10:42:00+08:00")
        # 即使理论可达，当前时间已过截止也不可行
        self.assertFalse(
            transfer_feasible(first, second, now=parse_time("2028-07-03T11:00:00+08:00"))
        )


class PlanLegTest(unittest.TestCase):
    def setUp(self):
        self.leg = make_leg(
            "B1", ["bay-4", "seat-5", "seat-6", "seat-8"],
            shared={"bay-4": ["seat-5", "seat-6"]},
        )
        self.members = [
            Member("p-21", Need.WHEELCHAIR, "old-2:p-21"),
            Member("p-22", Need.STANDARD, "old-2:p-22"),
        ]

    def test_blocked_shared_group_frees_only_seat_8(self):
        # seat-5 被外部占用 -> bay-4/seat-5/seat-6 整体不可用
        plan = plan_leg(self.leg, self.members, blocked_labels=["seat-5"])
        self.assertFalse(plan.ok)
        self.assertIn("p-21:no_wheelchair_bay", plan.reason)

    def test_plan_succeeds_when_bay_available(self):
        plan = plan_leg(self.leg, self.members, blocked_labels=[])
        self.assertTrue(plan.ok, plan.reason)
        self.assertEqual(plan.assignments["p-21"], "bay-4")
        self.assertIn(plan.assignments["p-22"], {"seat-5", "seat-6", "seat-8"})

    def test_existing_secured_assignment_is_reused(self):
        fixed = {"p-21": "bay-4"}
        plan = plan_leg(self.leg, self.members, blocked_labels=[], fixed=fixed)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.assignments["p-21"], "bay-4")

    def test_cancelled_leg_is_not_plannable(self):
        leg = LegConfig(
            self.leg.leg_id, self.leg.carrier, self.leg.step_free, self.leg.spaces,
            self.leg.conflict_groups, self.leg.boarding_cutoff, self.leg.arrives,
            cancelled=True,
        )
        plan = plan_leg(leg, self.members, blocked_labels=[])
        self.assertFalse(plan.ok)
        self.assertEqual(plan.reason, "leg_cancelled")


class FeasibilityTest(unittest.TestCase):
    def setUp(self):
        self.a1 = make_leg("A1", ["bay-1", "seat-2"], arrives="2028-07-03T10:35:00+08:00")
        self.b1 = make_leg(
            "B1", ["bay-4", "seat-5", "seat-6", "seat-8"],
            shared={"bay-4": ["seat-5", "seat-6"]},
            cutoff="2028-07-03T10:42:00+08:00",
        )
        self.b2 = make_leg(
            "B2", ["bay-7", "seat-18"], cutoff="2028-07-03T11:30:00+08:00"
        )
        self.legs = {leg.leg_id: leg for leg in (self.a1, self.b1, self.b2)}
        self.members = [
            Member("p-21", Need.WHEELCHAIR, "old-2:p-21"),
            Member("p-22", Need.STANDARD, "old-2:p-22"),
        ]

    def test_full_itinerary_feasible_on_b2_even_when_b1_bay_blocked(self):
        report = evaluate_itinerary(
            ["A1", "B2"], self.members, self.legs, [],
            external_occupied={"B1": ["seat-5"]},
        )
        self.assertTrue(report.ok, report.reasons)

    def test_b1_itinerary_infeasible_when_wheelchair_bay_shared_space_occupied(self):
        report = evaluate_itinerary(
            ["A1", "B1"], self.members, self.legs, [],
            external_occupied={"B1": ["seat-5"]},
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("B1" in reason for reason in report.reasons))

    def test_missed_cutoff_marks_itinerary_infeasible(self):
        delayed = LegConfig(
            self.a1.leg_id, self.a1.carrier, self.a1.step_free, self.a1.spaces,
            self.a1.conflict_groups, self.a1.boarding_cutoff,
            self.a1.arrives + timedelta(minutes=14), self.a1.cancelled,
        )
        legs = dict(self.legs, A1=delayed)
        report = evaluate_itinerary(["A1", "B1"], self.members, legs, [])
        self.assertFalse(report.ok)
        self.assertIn("A1->B1:missed_boarding_cutoff", report.reasons)

    def test_boarded_leg_is_not_replanned_but_still_covered(self):
        allocation = AllocationView(
            "alloc-1", "case-1", "p-21", "A1", "bay-1", SpaceKind.BAY,
            AllocState.BOARDED, "old-2:p-21",
        )
        report = evaluate_itinerary(
            ["A1", "B2"], self.members, self.legs, [allocation],
            covered_passengers=[],
        )
        self.assertTrue(report.ok, report.reasons)
        b2_plan = next(plan for plan in report.legs if plan.leg_id == "B2")
        self.assertEqual(b2_plan.assignments["p-21"], "bay-7")

    def test_split_requires_explicit_consent(self):
        report = evaluate_itinerary(
            ["A1", "B2"], self.members, self.legs, [],
            split_allowed=False, covered_passengers=["p-22"],
        )
        self.assertFalse(report.ok)
        self.assertIn("split_requires_consent", report.reasons)

        report_ok = evaluate_itinerary(
            ["A1", "B2"], self.members, self.legs, [],
            split_allowed=True, covered_passengers=["p-22"],
        )
        self.assertTrue(report_ok.ok, report_ok.reasons)


if __name__ == "__main__":
    unittest.main()
