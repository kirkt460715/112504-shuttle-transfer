"""应用服务测试：预占/断线、确认条件、释放转座、晚点重判与登车归集。"""
import unittest
from datetime import datetime, timezone

from rebook.app import RebookService, ServiceError
from rebook.carrier import FakeCarrier
from rebook.domain import AllocState, CaseState, Member, Need
from rebook.loader import load_registers
from rebook.store import MemoryRepository

FIXED_NOW = datetime(2028, 7, 3, 2, 30, tzinfo=timezone.utc)  # 10:30 +08:00


def clock():
    return FIXED_NOW


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.current = FIXED_NOW
        self.repo = MemoryRepository()
        self.carrier = FakeCarrier()
        self.service = RebookService(self.repo, self.carrier, clock=lambda: self.current)
        registers = {
            "passengers": ["p-21", "p-22"],
            "party": {"id": "group-3", "wheelchair": ["p-21"], "escort": "p-22",
                      "may_split": False},
            "original": {"ticket": "old-2", "leg": "mountain-direct", "status": "suspended"},
            "legs": [
                {"id": "A1", "arrives": "2028-07-03T10:35:00+08:00", "delay_min": 14,
                 "boarding_cutoff": "2028-07-03T09:50:00+08:00",
                 "physical_space": ["bay-1", "seat-2"]},
                {"id": "B1", "boarding_cutoff": "2028-07-03T10:42:00+08:00",
                 "physical_space": ["bay-4", "seat-8"],
                 "shared": {"bay-4": ["seat-5", "seat-6"]}},
                {"id": "B2", "boarding_cutoff": "2028-07-03T11:30:00+08:00",
                 "physical_space": ["bay-7", "seat-18"]},
            ],
            "filled": ["B1:seat-5"],
            "carrier_by_leg": {"A1": "local-a", "B1": "local-b", "B2": "local-b"},
            "candidate_itineraries": {
                "original-substitute": ["A1", "B1"],
                "late-substitute": ["A1", "B2"],
            },
        }
        self.summary = load_registers(self.repo, registers)
        self.case_id = "case-group-3"
        self.members = [
            Member("p-21", Need.WHEELCHAIR, "old-2:p-21"),
            Member("p-22", Need.STANDARD, "old-2:p-22"),
        ]

    def allocation(self, passenger_id, leg_id):
        return self.repo.find_allocation(self.case_id, passenger_id, leg_id)

    def script_b2_success(self):
        self.carrier.script_hold("local-b", "B2", "bay-7", "p-21", "hold-81")
        self.carrier.script_hold("local-b", "B2", "seat-18", "p-22", "hold-82")
        self.carrier.script_boarding("local-a", "A1", "age-31", "p-21", "bay-1")


class InfeasibleCaseTest(ServiceTestBase):
    def test_b1_cannot_be_confirmed_when_wheelchair_space_blocked(self):
        status = self.service.confirm_case(self.case_id, itinerary=["A1", "B1"])
        self.assertEqual(status["state"], CaseState.PENDING.value)
        self.assertFalse(status["feasible"])
        self.assertTrue(any("B1" in reason for reason in status["reasons"]))

    def test_wheelchair_passenger_never_gets_regular_seat(self):
        # 即使把 B2 的轮椅位外部占掉，也不会把 p-21 安排到 seat-18
        self.repo.upsert_external_occupancy(
            "B2", "bay-7", "occupied", "blocked", FIXED_NOW
        )
        status = self.service.confirm_case(self.case_id, itinerary=["A1", "B2"])
        self.assertEqual(status["state"], CaseState.PENDING.value)
        self.assertTrue(
            any("no_wheelchair_bay" in reason for reason in status["reasons"]),
            status["reasons"],
        )
        self.assertIsNone(self.allocation("p-21", "B2"))


class ConfirmationTest(ServiceTestBase):
    def _board_first_leg(self):
        # 首段 A1 已过最晚登车时间：p-21 已登车，p-22 尚未登车
        self.carrier.script_boarding("local-a", "A1", "age-31", "p-21", "bay-1")
        self.service.ingest_boarding("age-31", "local-a", "A1", op_id="op-age-31")

    def test_full_itinerary_confirmed_only_when_every_leg_secured(self):
        self.script_b2_success()
        # A1 仍有成员未登车时，不能发出整段确认
        premature = self.service.confirm_case(self.case_id, itinerary=["A1", "B2"])
        self.assertEqual(premature["state"], CaseState.PENDING.value)
        self.assertTrue(
            any("closed_leg_without_boarding" in reason for reason in premature["reasons"])
        )

        self._board_first_leg()
        # p-22 仍未登 A1，但同行组已明确改走 B2 接驳：其 A1 段由值班台按未到处理，
        # 全程确认只在所有“仍可保障”的段齐备后成立。这里模拟 p-22 同样完成首段登车。
        self.carrier.script_boarding("local-a", "A1", "age-32", "p-22", "seat-2")
        self.service.ingest_boarding("age-32", "local-a", "A1", op_id="op-age-32")

        status = self.service.confirm_case(self.case_id, itinerary=["A1", "B2"])
        self.assertEqual(status["state"], CaseState.CONFIRMED.value)
        self.assertTrue(status["feasible"])
        self.assertEqual(self.allocation("p-21", "B2").state, AllocState.HELD)
        self.assertEqual(self.allocation("p-22", "B2").state, AllocState.HELD)
        self.assertEqual(self.allocation("p-21", "B2").space_label, "bay-7")

    def test_idempotent_request_returns_same_confirmation(self):
        self.script_b2_success()
        self._board_first_leg()
        self.carrier.script_boarding("local-a", "A1", "age-32", "p-22", "seat-2")
        self.service.ingest_boarding("age-32", "local-a", "A1", op_id="op-age-32")
        first = self.service.confirm_case(
            self.case_id, itinerary=["A1", "B2"], request_id="req-1"
        )
        second = self.service.confirm_case(
            self.case_id, itinerary=["A1", "B2"], request_id="req-1"
        )
        self.assertEqual(first, second)
        # 承运方只应收到每个座位一次占座请求
        hold_calls = [call for call in self.carrier.calls if call["operation"] == "hold"]
        self.assertEqual(len(hold_calls), 2)


class DisconnectTest(ServiceTestBase):
    def _board_first_leg(self):
        self.carrier.script_boarding("local-a", "A1", "age-31", "p-21", "bay-1")
        self.carrier.script_boarding("local-a", "A1", "age-32", "p-22", "seat-2")
        self.service.ingest_boarding("age-31", "local-a", "A1", op_id="op-age-31")
        self.service.ingest_boarding("age-32", "local-a", "A1", op_id="op-age-32")

    def test_disconnected_hold_keeps_pre_claim_and_blocks_other_passenger(self):
        self._board_first_leg()
        # p-21 的 B2 轮椅位占座在对端落库后断线
        self.carrier.script_hold(
            "local-b", "B2", "bay-7", "p-21", "hold-81", drop=True
        )
        self.carrier.script_hold("local-b", "B2", "seat-18", "p-22", "hold-82")
        status = self.service.confirm_case(self.case_id, itinerary=["A1", "B2"])
        self.assertEqual(status["state"], CaseState.WAITING_DESK.value)
        pre_claim = self.allocation("p-21", "B2")
        self.assertEqual(pre_claim.state, AllocState.PRE_CLAIMED)
        self.assertIsNotNone(pre_claim.request_op_id)

        # 结果未明期间，另一同行组不能拿到同一物理空间
        other_members = [Member("p-99", Need.WHEELCHAIR, "ticket-99")]
        self.service.open_case("case-9", ["B2"], other_members)
        with self.assertRaises(ServiceError):
            self.service.confirm_case("case-9")

        # 值班台用同一操作编号补查/重投：对端日志显示占座成功 -> 转正式占座
        reconciled = self.service.reconcile_operation(pre_claim.request_op_id)
        self.assertEqual(reconciled["state"], "held")
        self.assertEqual(
            self.allocation("p-21", "B2").state, AllocState.HELD
        )
        status = self.service.case_status(self.case_id)
        self.assertEqual(status["state"], CaseState.CONFIRMED.value)

    def test_reconcile_not_accepted_releases_pre_claim(self):
        self._board_first_leg()
        key = ("hold", "local-b", "B2:bay-7:p-21")
        self.carrier.script_hold(
            "local-b", "B2", "bay-7", "p-21", "hold-81", drop=True
        )
        self.carrier.script_hold("local-b", "B2", "seat-18", "p-22", "hold-82")
        self.service.confirm_case(self.case_id, itinerary=["A1", "B2"])
        op_id = self.allocation("p-21", "B2").request_op_id
        # 模拟对端其实没有接单：清掉已落库日志与应答脚本，重投被明确拒绝 -> 预占作废
        del self.carrier.script[key]
        self.carrier.journal.pop(op_id, None)
        reconciled = self.service.reconcile_operation(op_id)
        self.assertEqual(reconciled["state"], "not_accepted")
        self.assertEqual(self.allocation("p-21", "B2").state, AllocState.FAILED)

        # 物理空间解锁后，另一位轮椅旅客可以拿到 bay-7
        self.carrier.script_hold("local-b", "B2", "bay-7", "p-99", "hold-99")
        self.service.open_case("case-9", ["B2"], [Member("p-99", Need.WHEELCHAIR, "t-99")])
        status = self.service.confirm_case("case-9")
        self.assertEqual(status["state"], CaseState.CONFIRMED.value)

    def test_explicit_carrier_rejection_marks_failed_and_keeps_case_pending(self):
        self._board_first_leg()
        self.carrier.script_hold(
            "local-b", "B2", "bay-7", "p-21", "hold-81", reject=True
        )
        self.carrier.script_hold("local-b", "B2", "seat-18", "p-22", "hold-82")
        status = self.service.confirm_case(self.case_id, itinerary=["A1", "B2"])
        self.assertEqual(status["state"], CaseState.PENDING.value)
        self.assertEqual(self.allocation("p-21", "B2").state, AllocState.FAILED)


class ReleaseTransferTest(ServiceTestBase):
    def _board_first_leg(self):
        self.carrier.script_boarding("local-a", "A1", "age-31", "p-21", "bay-1")
        self.carrier.script_boarding("local-a", "A1", "age-32", "p-22", "seat-2")
        self.service.ingest_boarding("age-31", "local-a", "A1", op_id="op-b1")
        self.service.ingest_boarding("age-32", "local-a", "A1", op_id="op-b2")

    def test_released_seat_can_transfer_only_when_whole_party_feasible(self):
        # 首段 A1 已关闭占座通道，两位旅客均已登车
        self._board_first_leg()
        # B2 轮椅位被外部占用，座位规划本身不可行：确认保持待处理且不发起对端调用
        self.repo.upsert_external_occupancy(
            "B2", "bay-7", "occupied", "other-party", self.current
        )
        blocked = self.service.confirm_case(self.case_id, itinerary=["A1", "B2"])
        self.assertEqual(blocked["state"], CaseState.PENDING.value)
        self.assertTrue(any("B2" in reason for reason in blocked["reasons"]))
        self.assertEqual(
            [call for call in self.carrier.calls if call["operation"] == "hold"], []
        )

        # 另一旅客取消，B2 轮椅位释放；此时承运方应答尚未编排，
        # 系统只能重估出“座位规划可行”，不能凭空发出确认
        event = self.service.record_space_event(
            "B2", "bay-7", "cancelled", "2028-07-03T10:04:00+08:00"
        )
        self.assertEqual(event["rechecked_cases"], [])
        status = self.service.case_status(self.case_id)
        self.assertEqual(status["state"], CaseState.PENDING.value)
        self.assertTrue(status["feasible"])

        # 编排 B2 应答后重新确认，余下各段与换乘条件都可兑现才 confirmed
        self.carrier.script_hold("local-b", "B2", "bay-7", "p-21", "hold-1")
        self.carrier.script_hold("local-b", "B2", "seat-18", "p-22", "hold-2")
        status = self.service.confirm_case(self.case_id, itinerary=["A1", "B2"])
        self.assertEqual(status["state"], CaseState.CONFIRMED.value)
        self.assertEqual(self.allocation("p-21", "B2").space_label, "bay-7")

    def test_release_event_auto_confirms_when_carrier_replies_are_ready(self):
        self._board_first_leg()
        self.repo.upsert_external_occupancy(
            "B2", "bay-7", "occupied", "other-party", self.current
        )
        # 提前编排好 B2 承运方应答；物理空间被占期间确认不会调用对端
        self.carrier.script_hold("local-b", "B2", "bay-7", "p-21", "hold-1")
        self.carrier.script_hold("local-b", "B2", "seat-18", "p-22", "hold-2")
        blocked = self.service.confirm_case(self.case_id, itinerary=["A1", "B2"])
        self.assertEqual(blocked["state"], CaseState.PENDING.value)
        self.assertEqual(
            [call for call in self.carrier.calls if call["operation"] == "hold"], []
        )
        # 释放事件触发自动重估：应答齐备且全程可兑现，直接转为已确认
        event = self.service.record_space_event(
            "B2", "bay-7", "cancelled", "2028-07-03T10:04:00+08:00"
        )
        self.assertIn(self.case_id, event["rechecked_cases"])
        self.assertEqual(
            self.service.case_status(self.case_id)["state"],
            CaseState.CONFIRMED.value,
        )


class DelayAndReplanTest(ServiceTestBase):
    def _board_first_leg(self):
        self.carrier.script_boarding("local-a", "A1", "age-31", "p-21", "bay-1")
        self.carrier.script_boarding("local-a", "A1", "age-32", "p-22", "seat-2")
        self.service.ingest_boarding("age-31", "local-a", "A1", op_id="op-age-31")
        self.service.ingest_boarding("age-32", "local-a", "A1", op_id="op-age-32")

    def test_delay_misses_b1_cutoff_and_replan_to_b2_keeps_boarded_leg(self):
        self.script_b2_success()
        # 两位旅客都已登上 A1（首段已关闭占座通道）
        self._board_first_leg()
        self.assertEqual(
            self.allocation("p-21", "A1").state, AllocState.BOARDED
        )

        # 前车晚点至 10:49，赶不上 B1 的 10:42 截止
        delay = self.service.report_delay(
            "A1", "2028-07-03T10:49:00+08:00"
        )
        case_view = next(item for item in delay["cases"] if item["case_id"] == self.case_id)
        self.assertTrue(
            any("missed_boarding_cutoff" in reason for reason in case_view["reasons"])
        )

        # 改到 B2：已登车的 A1 不会被重占，B2 两段保障齐备后才确认
        replanned = self.service.replan_case(self.case_id, ["A1", "B2"])
        self.assertEqual(replanned["state"], CaseState.CONFIRMED.value)
        self.assertEqual(self.allocation("p-21", "A1").state, AllocState.BOARDED)
        self.assertEqual(self.allocation("p-21", "B2").state, AllocState.HELD)
        # 没有对 A1 发起第二次占座
        a1_holds = [
            call for call in self.carrier.calls
            if call["operation"] == "hold" and call["request"]["leg"] == "A1"
        ]
        self.assertEqual(a1_holds, [])

    def test_replan_cannot_drop_a_boarded_leg(self):
        self.script_b2_success()
        self.carrier.script_boarding("local-a", "A1", "age-31", "p-21", "bay-1")
        self.service.ingest_boarding("age-31", "local-a", "A1", op_id="op-1")
        with self.assertRaises(ServiceError):
            self.service.replan_case(self.case_id, ["B2"])

    def test_old_held_seats_become_releasable_not_deleted_on_replan(self):
        from datetime import timezone
        # 首段 A1 已关闭占座：两位旅客先完成登车，B1 仍开放占座
        self.current = datetime(2028, 7, 3, 2, 0, tzinfo=timezone.utc)
        self.service.repo.record_leg_delay(
            "A1", "2028-07-03T10:35:00+08:00"
        )  # 取消台账里的预置晚点，先按准点确认 A1+B1
        self.carrier.script_boarding("local-a", "A1", "age-31", "p-21", "bay-1")
        self.carrier.script_boarding("local-a", "A1", "age-32", "p-22", "seat-2")
        self.service.ingest_boarding("age-31", "local-a", "A1", op_id="b1")
        self.service.ingest_boarding("age-32", "local-a", "A1", op_id="b2")
        # 先编排承运方应答，再发释放事件，避免自动重估时对端无应答
        self.carrier.script_hold("local-b", "B1", "bay-4", "p-21", "h1")
        self.carrier.script_hold("local-b", "B1", "seat-8", "p-22", "h2")
        event = self.service.record_space_event(
            "B1", "seat-5", "cancelled", "2028-07-03T10:04:00+08:00"
        )
        self.assertIn(self.case_id, event["rechecked_cases"])
        first = self.service.case_status(self.case_id)
        self.assertEqual(first["state"], CaseState.CONFIRMED.value)

        # 晚点后 B1 不可达，改到 B2；B1 占座转为可释放但仍占物理空间
        self.current = datetime(2028, 7, 3, 2, 30, tzinfo=timezone.utc)
        self.script_b2_success()
        self.service.report_delay("A1", "2028-07-03T10:49:00+08:00")
        replanned = self.service.replan_case(self.case_id, ["A1", "B2"])
        self.assertEqual(replanned["state"], CaseState.CONFIRMED.value)
        b1_allocations = [
            item for item in replanned["allocations"] if item["leg_id"] == "B1"
        ]
        self.assertTrue(b1_allocations)
        self.assertTrue(
            all(item["state"] == AllocState.RELEASABLE.value for item in b1_allocations)
        )
        # 值班台确认释放前，物理空间仍不能分给别人
        self.service.open_case("case-9", ["B1"], [Member("p-99", Need.WHEELCHAIR, "t-99")])
        with self.assertRaises(ServiceError):
            self.service.confirm_case("case-9")


class BoardingTest(ServiceTestBase):
    def _board_p21(self):
        self.carrier.script_boarding("local-a", "A1", "age-31", "p-21", "bay-1")
        return self.service.ingest_boarding(
            "age-31", "local-a", "A1", op_id="op-age-31"
        )

    def test_boarding_fact_is_idempotent_and_attributed_to_credential(self):
        first = self._board_p21()
        second = self.service.ingest_boarding(
            "age-31", "local-a", "A1", op_id="op-age-31"
        )
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["fact"], second["fact"])
        self.assertEqual(first["fact"]["credential"], "old-2:p-21")
        # 登车后该段不会再有占座调用
        a1_holds = [
            call for call in self.carrier.calls
            if call["operation"] == "hold" and call["request"]["leg"] == "A1"
        ]
        self.assertEqual(a1_holds, [])

    def test_late_boarding_creates_allocation_when_none_existed(self):
        fact = self._board_p21()
        self.assertEqual(fact["state"], "boarded")
        allocation = self.allocation("p-21", "A1")
        self.assertEqual(allocation.state, AllocState.BOARDED)
        self.assertEqual(allocation.space_label, "bay-1")


class ManifestAndRecoveryTest(ServiceTestBase):
    def _board_first_leg(self):
        self.carrier.script_boarding("local-a", "A1", "age-31", "p-21", "bay-1")
        self.carrier.script_boarding("local-a", "A1", "age-32", "p-22", "seat-2")
        self.service.ingest_boarding("age-31", "local-a", "A1", op_id="op-1")
        self.service.ingest_boarding("age-32", "local-a", "A1", op_id="op-2")

    def test_manifest_lists_reserved_boarded_and_releasable_without_state_change(self):
        self.script_b2_success()
        self._board_first_leg()
        self.service.confirm_case(self.case_id, itinerary=["A1", "B2"])

        manifest = self.service.carrier_manifest("local-b", "B2")
        self.assertEqual(
            {item["passenger_id"] for item in manifest["reserved"]},
            {"p-21", "p-22"},
        )
        a1_manifest = self.service.carrier_manifest("local-a", "A1")
        self.assertEqual(
            {item["passenger_id"] for item in a1_manifest["boarded"]},
            {"p-21", "p-22"},
        )
        # 重复拉取不改变状态（不二次确认）
        again = self.service.carrier_manifest("local-b", "B2")
        self.assertEqual(manifest, again)

    def test_no_show_then_release_returns_space_to_pool(self):
        self.script_b2_success()
        self._board_first_leg()
        self.service.confirm_case(self.case_id, itinerary=["A1", "B2"])
        allocation = self.allocation("p-22", "B2")
        no_show = self.service.mark_no_show("local-b", "B2", allocation.allocation_id)
        self.assertEqual(no_show["state"], AllocState.RELEASABLE.value)

        # 释放前其他组拿不到 seat-18
        self.service.open_case("case-9", ["B2"], [Member("p-99", Need.STANDARD, "t-99")])
        self.carrier.script_hold("local-b", "B2", "seat-18", "p-99", "h99")
        with self.assertRaises(ServiceError):
            self.service.confirm_case("case-9")

        released = self.service.acknowledge_release("local-b", allocation.allocation_id)
        self.assertEqual(released["state"], AllocState.RELEASED.value)
        status = self.service.confirm_case("case-9")
        self.assertEqual(status["state"], CaseState.CONFIRMED.value)

    def test_recovery_view_exposes_partial_case_unknown_ops_and_unreleased(self):
        self._board_first_leg()
        self.carrier.script_hold("local-b", "B2", "bay-7", "p-21", "hold-81", drop=True)
        self.carrier.script_hold("local-b", "B2", "seat-18", "p-22", "hold-82")
        self.service.confirm_case(self.case_id, itinerary=["A1", "B2"])
        recovery = self.service.desk_recovery()
        self.assertTrue(recovery["unknown_operations"])
        self.assertTrue(
            any(item["state"] == AllocState.PRE_CLAIMED.value
                for item in recovery["unreleased_spaces"])
        )
        open_cases = {item["case_id"] for item in recovery["partial_or_open_cases"]}
        self.assertIn(self.case_id, open_cases)


class PassengerQueryTest(ServiceTestBase):
    def test_passenger_sees_leg_states_and_wait_flag(self):
        self.carrier.script_boarding("local-a", "A1", "age-31", "p-21", "bay-1")
        self.carrier.script_boarding("local-a", "A1", "age-32", "p-22", "seat-2")
        self.service.ingest_boarding("age-31", "local-a", "A1", op_id="op-1")
        self.service.ingest_boarding("age-32", "local-a", "A1", op_id="op-2")
        self.carrier.script_hold("local-b", "B2", "bay-7", "p-21", "hold-81", drop=True)
        self.carrier.script_hold("local-b", "B2", "seat-18", "p-22", "hold-82")
        self.service.confirm_case(self.case_id, itinerary=["A1", "B2"])
        view = self.service.passenger_query("old-2:p-21")
        self.assertEqual(view["case_state"], CaseState.WAITING_DESK.value)
        self.assertTrue(view["must_wait_for_desk"])
        states = {leg["leg_id"]: leg["state"] for leg in view["legs"]}
        self.assertEqual(states["B2"], "waiting_desk")
        self.assertEqual(states["A1"], "boarded")

    def test_unknown_credential_is_404_level_error(self):
        with self.assertRaises(Exception):
            self.service.passenger_query("no-such-ticket")


class SqlitePersistenceTest(ServiceTestBase):
    def _make_repo(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from rebook.store import SqliteRepository

        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        return SqliteRepository(Path(self.tmp.name) / "db.sqlite3")

    def test_unique_index_blocks_double_booking_across_repository(self):
        repo = self._make_repo()
        load_registers(repo, {
            "legs": [{"id": "B2", "physical_space": ["bay-7", "seat-18"],
                      "boarding_cutoff": "2028-07-03T11:30:00+08:00"}],
            "carrier_by_leg": {"B2": "local-b"},
        })
        service = RebookService(repo, self.carrier, clock=clock)
        service.open_case("c1", ["B2"], [Member("p-1", Need.WHEELCHAIR, "t1")])
        service.open_case("c2", ["B2"], [Member("p-2", Need.WHEELCHAIR, "t2")])
        self.carrier.script_hold("local-b", "B2", "bay-7", "p-1", "h1", drop=True)
        service.confirm_case("c1")  # 留下 PRE_CLAIMED
        self.carrier.script_hold("local-b", "B2", "bay-7", "p-2", "h2")
        with self.assertRaises(ServiceError):
            service.confirm_case("c2")
        repo.close()

    def test_state_survives_repository_restart(self):
        repo = self._make_repo()
        service = RebookService(repo, self.carrier, clock=clock)
        load_registers(repo, {
            "passengers": ["p-21", "p-22"],
            "party": {"id": "g1", "wheelchair": ["p-21"], "escort": "p-22"},
            "original": {"ticket": "old-2"},
            "legs": [{"id": "B2", "physical_space": ["bay-7", "seat-18"],
                      "boarding_cutoff": "2028-07-03T11:30:00+08:00"}],
            "carrier_by_leg": {"B2": "local-b"},
            "candidate_itineraries": {"only": ["B2"]},
        })
        self.carrier.script_hold("local-b", "B2", "bay-7", "p-21", "h1")
        self.carrier.script_hold("local-b", "B2", "seat-18", "p-22", "h2")
        service.confirm_case("case-g1")
        path = repo.path
        repo.close()

        from rebook.store import SqliteRepository
        restarted = SqliteRepository(path)
        self.addCleanup(restarted.close)
        service2 = RebookService(restarted, self.carrier, clock=clock)
        status = service2.case_status("case-g1")
        self.assertEqual(status["state"], CaseState.CONFIRMED.value)
        recovery = service2.desk_recovery()
        self.assertIsInstance(recovery["unknown_operations"], list)


if __name__ == "__main__":
    unittest.main()


class PassengerDecisionTest(ServiceTestBase):
    def _board_first_leg(self):
        self.carrier.script_boarding("local-a", "A1", "age-31", "p-21", "bay-1")
        self.carrier.script_boarding("local-a", "A1", "age-32", "p-22", "seat-2")
        self.service.ingest_boarding("age-31", "local-a", "A1", op_id="op-31")
        self.service.ingest_boarding("age-32", "local-a", "A1", op_id="op-32")

    def test_retaining_passenger_gets_no_new_hold(self):
        self._board_first_leg()
        # p-22 选择保留原确认、等待新方案
        self.service.record_passenger_decision(
            self.case_id, "p-22", "retain"
        )
        self.carrier.script_hold("local-b", "B2", "bay-7", "p-21", "h21")
        self.carrier.script_hold("local-b", "B2", "seat-18", "p-22", "h22")
        status = self.service.confirm_case(self.case_id, itinerary=["A1", "B2"])
        # 只有 p-21 被保障；p-22 保留原安排，方案不能称为整段已确认
        self.assertNotEqual(status["state"], CaseState.CONFIRMED.value)
        self.assertIsNotNone(self.allocation("p-21", "B2"))
        self.assertIsNone(self.allocation("p-22", "B2"))
        b22_calls = [
            call for call in self.carrier.calls
            if call["operation"] == "hold" and call["request"]["passenger"] == "p-22"
        ]
        self.assertEqual(b22_calls, [])

    def test_split_consent_allows_subset_coverage(self):
        # 打开方案的拆分开关，p-22 明确同意拆分并先成行
        self.repo.cases[self.case_id]["split_allowed"] = True
        self.service.record_passenger_decision(
            self.case_id, "p-22", "split", split_allowed=True
        )
        # 已关闭占座的 A1 上，两位旅客都要有登车事实
        self.carrier.script_boarding("local-a", "A1", "age-31", "p-21", "bay-1")
        self.carrier.script_boarding("local-a", "A1", "age-32", "p-22", "seat-2")
        self.service.ingest_boarding("age-31", "local-a", "A1", op_id="op-31")
        self.service.ingest_boarding("age-32", "local-a", "A1", op_id="op-32")
        members = self.repo.list_members(self.case_id)
        report = self.service.check_feasibility(
            ["A1", "B2"], members, case_id=self.case_id,
            covered_passengers=["p-22"],
        )
        self.assertTrue(report.ok, report.reasons)

    def test_invalid_decision_rejected(self):
        with self.assertRaises(ServiceError):
            self.service.record_passenger_decision(
                self.case_id, "p-21", "maybe"
            )

    def test_decision_survives_in_case_status(self):
        self.service.record_passenger_decision(self.case_id, "p-21", "accept")
        status = self.service.case_status(self.case_id)
        decisions = {m["passenger_id"]: m["decision"] for m in status["members"]}
        self.assertEqual(decisions["p-21"], "accept")
        self.assertIsNone(decisions["p-22"])
