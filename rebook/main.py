"""改签后端服务启动入口。

示例：

    python3 -m rebook.main --db /tmp/rebook.sqlite3 \
        --registers fixtures/registers.json --port 8080

重启时保留 ``--db`` 文件即可恢复全部方案、结果未明操作与未释放座席；
对端桩（``partner_stub.py``）可独立保持运行。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .app import RebookService
from .carrier import HttpCarrier
from .loader import load_registers_file
from .store import SqliteRepository
from .web import build_server

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", type=Path, default=Path("/tmp/rebook-demo.sqlite3"))
    parser.add_argument(
        "--registers", type=Path, default=ROOT / "fixtures" / "registers.json"
    )
    parser.add_argument("--partner-host", default="127.0.0.1")
    parser.add_argument("--partner-port", type=int, default=8765)
    parser.add_argument("--no-load", action="store_true", help="不加载演练台账")
    args = parser.parse_args()

    repository = SqliteRepository(args.db)
    if not args.no_load and args.registers.exists():
        summary = load_registers_file(repository, args.registers)
        print(f"已引导台账：{summary['legs']}，方案 {summary.get('opened_case', '已存在')}")
    carrier = HttpCarrier(args.partner_host, args.partner_port)
    service = RebookService(repository, carrier)
    server = build_server(args.host, args.port, service)
    print(
        f"改签服务监听 http://{args.host}:{server.server_port}，"
        f"对端 http://{args.partner_host}:{args.partner_port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        repository.close()


if __name__ == "__main__":
    main()
