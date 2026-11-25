"""承运方端口。

- ``CarrierPort`` 定义业务服务依赖的两种对端操作：占座 ``hold`` 与登车回报 ``boarding``。
- ``HttpCarrier`` 对接 ``partner_stub.py`` 的 ``/operations`` 协议：同一操作编号
  重试返回原回报；``commit_disconnect`` 时首次连接被对端关闭，需凭编号走
  ``GET /operations/<id>`` 补查，补查前业务上必须视为“可能仍占用”。
- ``FakeCarrier`` 是测试用内存实现，可编排成功、断线与明确拒绝。
"""
from __future__ import annotations

import json
from http.client import HTTPConnection, RemoteDisconnected
from typing import Optional, Protocol, runtime_checkable


class CarrierError(Exception):
    """承运方调用异常基类。"""


class CarrierUnavailable(CarrierError):
    """结果未明：对端可能已接单占座，不能当作失败释放物理空间。"""


class CarrierRejected(CarrierError):
    """对端明确拒绝（如 422 无此样例、409 编号冲突），该段占座确定不成立。"""


@runtime_checkable
class CarrierPort(Protocol):
    def hold(
        self, op_id: str, carrier: str, leg_id: str, space_label: str, passenger_id: str
    ) -> dict: ...

    def fetch_boarding(self, op_id: str, carrier: str, leg_id: str, scan_id: str) -> dict: ...

    def query_operation(self, op_id: str) -> Optional[dict]: ...


def _post(base_host: str, base_port: int, payload: dict, timeout: float) -> dict:
    conn = HTTPConnection(base_host, base_port, timeout=timeout)
    try:
        conn.request(
            "POST", "/operations", json.dumps(payload, ensure_ascii=False),
            {"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        if response.status == 200:
            return body["reply"]
        if response.status in (422, 409, 400):
            raise CarrierRejected(f"status {response.status}: {body}")
        raise CarrierUnavailable(f"status {response.status}: {body}")
    except CarrierError:
        raise
    except (RemoteDisconnected, ConnectionError, OSError, json.JSONDecodeError) as exc:
        # 对端可能在写入接单日志后断连（commit_disconnect），结果未明。
        raise CarrierUnavailable(str(exc)) from exc
    finally:
        conn.close()


def _get(base_host: str, base_port: int, op_id: str, timeout: float) -> Optional[dict]:
    conn = HTTPConnection(base_host, base_port, timeout=timeout)
    try:
        conn.request("GET", f"/operations/{op_id}")
        response = conn.getresponse()
        if response.status == 404:
            return None
        body = json.loads(response.read().decode("utf-8"))
        if response.status != 200:
            raise CarrierUnavailable(f"status {response.status}: {body}")
        return body["reply"]
    except (RemoteDisconnected, ConnectionError, OSError, json.JSONDecodeError) as exc:
        raise CarrierUnavailable(str(exc)) from exc
    finally:
        conn.close()


class HttpCarrier:
    """通过合作方本地 HTTP 桩（或同协议真实服务）通信的客户端。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def _call(self, op_id: str, operation: str, request: dict) -> dict:
        payload = {"id": op_id, "operation": operation, "request": request}
        return _post(self.host, self.port, payload, self.timeout)

    def hold(self, op_id, carrier, leg_id, space_label, passenger_id) -> dict:
        return self._call(
            op_id, "hold",
            {"carrier": carrier, "leg": leg_id, "space": space_label, "passenger": passenger_id},
        )

    def fetch_boarding(self, op_id, carrier, leg_id, scan_id) -> dict:
        return self._call(
            op_id, "boarding",
            {"carrier": carrier, "leg": leg_id, "scan": scan_id},
        )

    def query_operation(self, op_id: str) -> Optional[dict]:
        return _get(self.host, self.port, op_id, self.timeout)


class FakeCarrier:
    """内存承运方：按请求键编排应答，支持断线注入与编号幂等。"""

    def __init__(self):
        self.script: dict[tuple[str, str, str], dict] = {}
        self.disconnect: set[tuple[str, str, str]] = set()
        self.reject: set[tuple[str, str, str]] = set()
        self.journal: dict[str, dict] = {}
        self.calls: list[dict] = []

    def script_hold(self, carrier: str, leg_id: str, space_label: str,
                    passenger_id: str, remote_hold: str, *, drop: bool = False,
                    reject: bool = False) -> None:
        key = ("hold", carrier, f"{leg_id}:{space_label}:{passenger_id}")
        self.script[key] = {"remote_hold": remote_hold, "state": "held"}
        if drop:
            self.disconnect.add(key)
        if reject:
            self.reject.add(key)

    def script_boarding(self, carrier: str, leg_id: str, scan_id: str,
                        passenger_id: str, physical_space: str) -> None:
        key = ("boarding", carrier, f"{leg_id}:{scan_id}")
        self.script[key] = {
            "passenger": passenger_id, "physical_space": physical_space, "state": "boarded",
        }

    @staticmethod
    def _key_for(operation: str, request: dict) -> tuple[str, str, str]:
        if operation == "hold":
            return ("hold", request["carrier"],
                    f"{request['leg']}:{request['space']}:{request['passenger']}")
        return ("boarding", request["carrier"], f"{request['leg']}:{request['scan']}")

    def _call(self, op_id: str, operation: str, request: dict) -> dict:
        self.calls.append({"op_id": op_id, "operation": operation, "request": dict(request)})
        if op_id in self.journal:
            previous = self.journal[op_id]
            if (previous["operation"], previous["request"]) != (operation, request):
                raise CarrierRejected("operation_id_conflict")
            return previous["reply"]
        key = self._key_for(operation, request)
        if key in self.reject:
            raise CarrierRejected("rejected by carrier")
        reply = self.script.get(key)
        if reply is None:
            raise CarrierRejected("unsupported_example")
        if key in self.disconnect:
            # 与 HTTP 桩的 commit_disconnect 一致：对端已落库但首次连接被关闭。
            # 落库后移除断线标记，使同一编号的后续重投能够拿回原始回报，
            # 而不会产生第二个占座。
            self.disconnect.discard(key)
            self.journal[op_id] = {
                "operation": operation, "request": dict(request), "reply": reply
            }
            raise CarrierUnavailable("commit_disconnect")
        self.journal[op_id] = {"operation": operation, "request": dict(request), "reply": reply}
        return reply

    def hold(self, op_id, carrier, leg_id, space_label, passenger_id) -> dict:
        return self._call(
            op_id, "hold",
            {"carrier": carrier, "leg": leg_id, "space": space_label, "passenger": passenger_id},
        )

    def fetch_boarding(self, op_id, carrier, leg_id, scan_id) -> dict:
        return self._call(
            op_id, "boarding",
            {"carrier": carrier, "leg": leg_id, "scan": scan_id},
        )

    def query_operation(self, op_id: str) -> Optional[dict]:
        recorded = self.journal.get(op_id)
        return dict(recorded["reply"]) if recorded else None
