"""HTTP 接口端到端测试：旅客、售票台、承运方三类角色视图。"""
import json
import unittest
from datetime import datetime, timezone
from http.client import HTTPConnection
from threading import Thread

from rebook.app import RebookService
from rebook.carrier import FakeCarrier
from rebook.domain import AllocState, CaseState
from rebook.loader import load_registers
from rebook.store import MemoryRepository
from rebook.web import build_server

FIXED_NOW = datetime(2028, 7, 3, 2, 30, tzinfo=timezone.utc)


class WebApiTest(unittest.TestCase):
    def setUp(self):
        self.current = FIXED_NOW
        self.repo = MemoryRepository()
        self.carrier = FakeCarrier()
        self.service = RebookService(self.repo, self.carrier, clock=lambda: self.current)
        registers = {
            "passengers": ["p-21", "p-22"],
            "party": {"id": "group-3", "wheelchair": ["p-21"], "escort": "p-22",
                      "may_split": False},
            "original": {"ticket": "old-2"},
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
            "candidate_itineraries": {"late": ["A1", "B2"]},
        }
        load_registers(self.repo, registers)
        self.server = build_server("127.0.0.1", 0, self.service)
        self.addCleanup(self.server.server_close)
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
                        daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port

    def call(self, method, path, data=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request(
                method, path,
                None if data is None else json.dumps(data, ensure_ascii=False),
                {"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()

    def _board(self):
        self.carrier.script_boarding("local-a", "A1", "age-31", "p-21", "bay-1")
        self.carrier.script_boarding("local-a", "A1", "age-32", "p-22", "seat-2")
        status, _ = self.call("POST", "/carrier/local-a/boarding",
                              {"scan_id": "age-31", "leg_id": "A1"})
        self.assertEqual(status, 200)
        status, _ = self.call("POST", "/carrier/local-a/boarding",
                              {"scan_id": "age-32", "leg_id": "A1"})
        self.assertEqual(status, 200)

    def test_health_and_passenger_404(self):
        status, body = self.call("GET", "/health")
        self.assertEqual((status, body["status"]), (200, "ok"))
        status, body = self.call("GET", "/passenger/itinerary/no-such")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_full_desk_carrier_passenger_flow(self):
        self._board()
        # 售票台视角：首段登车后方案为部分成行，后续段待保障
        status, cases = self.call("GET", "/desk/cases")
        self.assertEqual(status, 200)
        self.assertIn(
            cases["cases"][0]["state"],
            {CaseState.PENDING.value, CaseState.PARTIAL.value},
        )

        # 承运方应答编排：B2 一段断线、一段成功
        self.carrier.script_hold("local-b", "B2", "bay-7", "p-21", "hold-81", drop=True)
        self.carrier.script_hold("local-b", "B2", "seat-18", "p-22", "hold-82")
        status, body = self.call("POST", "/desk/cases/case-group-3/confirm",
                                 {"itinerary": ["A1", "B2"], "request_id": "req-c1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], CaseState.WAITING_DESK.value)

        # 旅客查询：必须等待售票台处理
        status, view = self.call("GET", "/passenger/itinerary/old-2:p-21")
        self.assertEqual(status, 200)
        self.assertTrue(view["must_wait_for_desk"])

        # 值班台恢复视图能看到结果未明操作
        status, recovery = self.call("GET", "/desk/recovery")
        self.assertEqual(status, 200)
        self.assertTrue(recovery["unknown_operations"])
        op_id = recovery["unknown_operations"][0]["op_id"]

        # 补查后方案确认
        status, reconciled = self.call("GET", f"/desk/operations/{op_id}")
        self.assertEqual(status, 200)
        self.assertEqual(reconciled["state"], "held")
        status, case_view = self.call("GET", "/desk/cases/case-group-3")
        self.assertEqual(case_view["state"], CaseState.CONFIRMED.value)

        # 承运方发车清单
        status, manifest = self.call("GET", "/carrier/local-b/B2/manifest")
        self.assertEqual(status, 200)
        self.assertEqual(
            {item["passenger_id"] for item in manifest["reserved"]},
            {"p-21", "p-22"},
        )

        # 旅客再次查询：两段可乘，无需等待
        status, view = self.call("GET", "/passenger/itinerary/old-2:p-21")
        self.assertFalse(view["must_wait_for_desk"])
        self.assertEqual(
            {leg["leg_id"]: leg["state"] for leg in view["legs"]},
            {"A1": "boarded", "B2": "bookable"},
        )

    def test_no_show_and_release_flow(self):
        self._board()
        self.carrier.script_hold("local-b", "B2", "bay-7", "p-21", "h1")
        self.carrier.script_hold("local-b", "B2", "seat-18", "p-22", "h2")
        self.call("POST", "/desk/cases/case-group-3/confirm",
                  {"itinerary": ["A1", "B2"]})
        _, case_view = self.call("GET", "/desk/cases/case-group-3")
        allocation = next(
            item for item in case_view["allocations"]
            if item["passenger_id"] == "p-22" and item["leg_id"] == "B2"
        )
        # 驾驶员发车时标记未到
        status, no_show = self.call(
            "POST", f"/carrier/local-b/B2/allocations/no-show",
            {"allocation_id": allocation["allocation_id"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(no_show["state"], AllocState.RELEASABLE.value)
        # 值班台确认释放
        status, released = self.call(
            "POST", "/carrier/local-b/allocations/release",
            {"allocation_id": allocation["allocation_id"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(released["state"], AllocState.RELEASED.value)

    def test_delay_endpoint_flags_missed_cutoff(self):
        self._board()
        # 恢复准点到达，使 A1+B1 的换乘条件先成立
        self.repo.record_leg_delay("A1", "2028-07-03T10:35:00+08:00")
        # 腾出 B1 共享空间并编排承运方应答
        self.call("POST", "/desk/space-events",
                  {"leg_id": "B1", "space_label": "seat-5", "reason": "cancelled",
                   "at": "2028-07-03T10:04:00+08:00"})
        self.carrier.script_hold("local-b", "B1", "bay-4", "p-21", "hb1")
        self.carrier.script_hold("local-b", "B1", "seat-8", "p-22", "hb2")
        status, replanned = self.call(
            "POST", "/desk/cases/case-group-3/replan",
            {"itinerary": ["A1", "B1"], "request_id": "replan-b1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(replanned["state"], CaseState.CONFIRMED.value)
        # 前车晚点至 10:49，赶不上 B1 的 10:42 截止
        status, delay = self.call(
            "POST", "/desk/delays",
            {"leg_id": "A1", "arrives": "2028-07-03T10:49:00+08:00"},
        )
        self.assertEqual(status, 200)
        case_view = next(
            item for item in delay["cases"] if item["case_id"] == "case-group-3"
        )
        self.assertTrue(
            any("missed_boarding_cutoff" in reason for reason in case_view["reasons"])
        )

    def test_idempotent_desk_request(self):
        self._board()
        self.carrier.script_hold("local-b", "B2", "bay-7", "p-21", "h1")
        self.carrier.script_hold("local-b", "B2", "seat-18", "p-22", "h2")
        payload = {"itinerary": ["A1", "B2"], "request_id": "idem-1"}
        _, first = self.call("POST", "/desk/cases/case-group-3/confirm", payload)
        _, second = self.call("POST", "/desk/cases/case-group-3/confirm", payload)
        self.assertEqual(first, second)
        hold_calls = [c for c in self.carrier.calls if c["operation"] == "hold"]
        self.assertEqual(len(hold_calls), 2)


if __name__ == "__main__":
    unittest.main()


class WebDecisionTest(WebApiTest):
    def test_passenger_decision_endpoint(self):
        status, body = self.call(
            "POST", "/desk/cases/case-group-3/decision",
            {"passenger_id": "p-22", "decision": "retain", "request_id": "dec-1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["recorded_decision"], "retain")
        decisions = {m["passenger_id"]: m["decision"] for m in body["members"]}
        self.assertEqual(decisions["p-22"], "retain")
        # 幂等重放
        status, again = self.call(
            "POST", "/desk/cases/case-group-3/decision",
            {"passenger_id": "p-22", "decision": "retain", "request_id": "dec-1"},
        )
        self.assertEqual(again["recorded_decision"], "retain")
        # 非法决定被拒
        status, body = self.call(
            "POST", "/desk/cases/case-group-3/decision",
            {"passenger_id": "p-22", "decision": "maybe"},
        )
        self.assertEqual(status, 422)
