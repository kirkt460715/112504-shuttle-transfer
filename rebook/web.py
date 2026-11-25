"""面向旅客、售票台（值班台）和承运方的 HTTP 接口。

仅使用标准库 ``http.server``。角色通过路径前缀区分：

- ``/passenger/...``  旅客凭自己的凭证查询
- ``/desk/...``       售票台/值班台操作与恢复
- ``/carrier/...``    承运方回报登车、拉取发车清单、确认释放

所有写操作接受可选 ``request_id`` 实现调用方幂等。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from .app import NotFound, RebookService, ServiceError
from .domain import Member, Need
from .store import Conflict


def _dump(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


class RebookHandler(BaseHTTPRequestHandler):
    server: "RebookHttpServer"

    def _send(self, status: int, value: Any) -> None:
        payload = _dump(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 1_048_576:
            raise ValueError("empty or oversized body")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        route = urlparse(self.path).path.strip("/").split("/")
        try:
            if route == ["health"]:
                self._send(200, {"status": "ok"})
            elif len(route) == 3 and route[0] == "passenger" and route[1] == "itinerary":
                self._send(200, self.service.passenger_query(route[2]))
            elif route == ["desk", "cases"]:
                self._send(200, {"cases": self.service.list_cases()})
            elif len(route) == 3 and route[0] == "desk" and route[1] == "cases":
                self._send(200, self.service.case_status(route[2]))
            elif route == ["desk", "recovery"]:
                self._send(200, self.service.desk_recovery())
            elif len(route) == 4 and route[0] == "carrier" and route[3] == "manifest":
                self._send(200, self.service.carrier_manifest(route[1], route[2]))
            elif len(route) == 3 and route[0] == "desk" and route[1] == "operations":
                self._send(200, self.service.reconcile_operation(route[2]))
            else:
                self._send(404, {"error": "not_found"})
        except NotFound as exc:
            self._send(404, {"error": "not_found", "detail": str(exc)})
        except ServiceError as exc:
            self._send(422, {"error": "service_error", "detail": str(exc)})
        except Exception as exc:  # pragma: no cover - 兜底
            self._send(500, {"error": "internal_error", "detail": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path.strip("/").split("/")
        try:
            body = self._body()
            response = self._route_post(route, body)
            self._send(200, response)
        except NotFound as exc:
            self._send(404, {"error": "not_found", "detail": str(exc)})
        except (ServiceError, Conflict, ValueError, KeyError, TypeError) as exc:
            self._send(422, {"error": "service_error", "detail": str(exc)})
        except Exception as exc:  # pragma: no cover - 兜底
            self._send(500, {"error": "internal_error", "detail": str(exc)})

    def _route_post(self, route: list[str], body: dict) -> Any:
        service = self.service
        request_id = body.get("request_id") or f"req-{uuid4().hex[:12]}"

        if route == ["desk", "cases"]:
            members = [
                Member(
                    passenger_id=item["passenger_id"],
                    need=Need(item.get("need", "standard")),
                    credential=item["credential"],
                )
                for item in body["members"]
            ]
            return service.open_case(
                body.get("case_id", f"case-{uuid4().hex[:10]}"),
                list(body["itinerary"]),
                members,
                party_id=body.get("party_id"),
                split_allowed=bool(body.get("split_allowed", False)),
                request_id=request_id,
            )

        if len(route) == 4 and route[0] == "desk" and route[1] == "cases" and route[3] == "confirm":
            return service.confirm_case(
                route[2], itinerary=body.get("itinerary"), request_id=request_id
            )

        if len(route) == 4 and route[0] == "desk" and route[1] == "cases" and route[3] == "replan":
            return service.replan_case(
                route[2], list(body["itinerary"]), request_id=request_id
            )

        if (
            len(route) == 4 and route[0] == "desk" and route[1] == "cases"
            and route[3] == "decision"
        ):
            return service.record_passenger_decision(
                route[2], body["passenger_id"], body["decision"],
                split_allowed=body.get("split_allowed"), request_id=request_id,
            )

        if route == ["desk", "delays"]:
            return service.report_delay(body["leg_id"], body["arrives"])

        if route == ["desk", "space-events"]:
            return service.record_space_event(
                body["leg_id"], body["space_label"], body["reason"], body["at"],
                request_id=request_id,
            )

        if len(route) == 3 and route[0] == "carrier" and route[2] == "boarding":
            return service.ingest_boarding(
                scan_id=body["scan_id"],
                carrier=route[1],
                leg_id=body["leg_id"],
                credential=body.get("credential"),
                op_id=body.get("op_id"),
                request_id=request_id,
            )

        if (
            len(route) == 5 and route[0] == "carrier"
            and route[3] == "allocations" and route[4] == "no-show"
        ):
            return service.mark_no_show(route[1], route[2], body["allocation_id"])

        if (
            len(route) == 4 and route[0] == "carrier"
            and route[2] == "allocations" and route[3] == "release"
        ):
            return service.acknowledge_release(route[1], body["allocation_id"])

        return self._raise_not_found(route)

    @staticmethod
    def _raise_not_found(route: list[str]):
        raise NotFound(f"unknown route: {'/'.join(route)}")

    @property
    def service(self) -> RebookService:
        return self.server.service

    def log_message(self, *args: Any) -> None:  # 静默，测试输出保持干净
        pass


class RebookHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: RebookService):
        self.service = service
        super().__init__(address, RebookHandler)


def build_server(host: str, port: int, service: RebookService) -> RebookHttpServer:
    return RebookHttpServer((host, port), service)
