"""与真实对端桩 ``partner_stub.py`` 的 HTTP 协议集成测试。

覆盖：

- ``commit_disconnect`` 后同一操作编号重投拿回原回报，不产生第二个占座；
- GET 探查、编号复用冲突（409）、未知样例（422）的协议语义；
- 业务服务经 HttpCarrier 完成 B2 两段占座与 A1 登车归集。
"""
import json
import unittest
from http.client import HTTPConnection, RemoteDisconnected
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from partner_stub import PartnerServer

from rebook.app import RebookService
from rebook.carrier import HttpCarrier
from rebook.domain import AllocState, CaseState
from rebook.loader import load_registers
from rebook.store import MemoryRepository

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class HttpPartnerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        journal = Path(self.tmp.name) / "partner-journal.sqlite3"
        self.server = PartnerServer(
            ("127.0.0.1", 0),
            FIXTURES / "partner-exchanges.json",
            journal,
        )
        self.addCleanup(self.server.server_close)
        self.thread = Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

        self.repo = MemoryRepository()
        registers = json.loads((FIXTURES / "registers.json").read_text(encoding="utf-8"))
        # 真实台账 A1 无 boarding_cutoff；补一个已过的截止，模拟首段占座通道关闭
        registers["legs"][0]["boarding_cutoff"] = "2028-07-03T09:50:00+08:00"
        load_registers(self.repo, registers)
        self.carrier = HttpCarrier(
            "127.0.0.1", self.server.server_port, timeout=3
        )
        self.service = RebookService(self.repo, self.carrier)
        self.case_id = "case-group-3"

    def request(self, method, path, data=None):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
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

    def test_commit_disconnect_then_redelivery_returns_original_hold(self):
        payload = {
            "id": "op-disconnect-1",
            "operation": "hold",
            "request": {
                "carrier": "local-b", "leg": "B2",
                "space": "bay-7", "passenger": "p-21",
            },
        }
        # 首次调用：对端已落库但断开连接
        with self.assertRaises(RemoteDisconnected):
            self.request("POST", "/operations", payload)
        # GET 探查可拿到已落库回报
        status, body = self.request("GET", "/operations/op-disconnect-1")
        self.assertEqual(status, 200)
        self.assertEqual(body["reply"]["remote_hold"], "hold-81")
        # 同编号同内容重投：返回原回报，不产生第二个占座
        status, body = self.request("POST", "/operations", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["reply"]["state"], "held")
        # 同编号不同内容：409 冲突
        tampered = {**payload, "request": {**payload["request"], "space": "bay-9"}}
        status, _ = self.request("POST", "/operations", tampered)
        self.assertEqual(status, 409)

    def test_unknown_example_is_rejected_not_silently_held(self):
        payload = {
            "id": "op-unknown-1",
            "operation": "hold",
            "request": {
                "carrier": "local-b", "leg": "B2",
                "space": "seat-99", "passenger": "p-21",
            },
        }
        status, body = self.request("POST", "/operations", payload)
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "unsupported_example")
        status, _ = self.request("GET", "/operations/op-unknown-1")
        self.assertEqual(status, 404)

    def test_service_confirms_late_substitute_through_real_partner(self):
        # 两位旅客首段登车（age-31 是对端台账中的真实扫码）
        boarding = self.service.ingest_boarding(
            "age-31", "local-a", "A1", op_id="op-board-31"
        )
        self.assertEqual(boarding["state"], "boarded")
        self.assertEqual(boarding["fact"]["credential"], "old-2:p-21")

        # p-22 的登车回报不在对端样例中：用已知的 A1 扫码事实补录
        # （真实部署由 local-a 回报；此处直接落库模拟其已上报）
        self.assertTrue(
            self.repo.record_boarding(
                "age-32", "p-22", "A1", "old-2:p-22", "seat-2",
                self.service.clock(),
            )
        )
        self.service._apply_boarding_to_case(
            "p-22", "A1", "seat-2", "old-2:p-22"
        )

        # 确认 A1+B2：B2 的 bay-7 占座样例配置为 commit_disconnect
        status = self.service.confirm_case(self.case_id, itinerary=["A1", "B2"])
        self.assertEqual(status["state"], CaseState.WAITING_DESK.value)
        pre_claim = next(
            item for item in status["allocations"]
            if item["passenger_id"] == "p-21" and item["leg_id"] == "B2"
        )
        self.assertEqual(pre_claim["state"], AllocState.PRE_CLAIMED.value)

        # 值班台补查断线操作：重投拿回 hold-81，方案转为已确认
        reconciled = self.service.reconcile_operation(pre_claim["request_op_id"])
        self.assertEqual(reconciled["state"], "held")
        status = self.service.case_status(self.case_id)
        self.assertEqual(status["state"], CaseState.CONFIRMED.value)

        # 承运方发车清单：B2 两个留座、A1 两个已上车
        b2_manifest = self.service.carrier_manifest("local-b", "B2")
        self.assertEqual(
            {item["passenger_id"] for item in b2_manifest["reserved"]},
            {"p-21", "p-22"},
        )
        a1_manifest = self.service.carrier_manifest("local-a", "A1")
        self.assertEqual(
            {item["passenger_id"] for item in a1_manifest["boarded"]},
            {"p-21", "p-22"},
        )

        # 旅客凭证查询：每段可乘且不再需要等待售票台
        view = self.service.passenger_query("old-2:p-21")
        self.assertFalse(view["must_wait_for_desk"])
        leg_states = {leg["leg_id"]: leg["state"] for leg in view["legs"]}
        self.assertEqual(leg_states, {"A1": "boarded", "B2": "bookable"})

    def test_carrier_cannot_see_other_carrier_manifest(self):
        with self.assertRaises(Exception):
            self.service.carrier_manifest("local-x", "B2")


if __name__ == "__main__":
    unittest.main()
