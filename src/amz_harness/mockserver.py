"""本地 mock 卖家后台（包内版本，供 CLI 与测试直接 import）。

用途：让整条链路（环境启动 → 登录 → 2FA → 站点切换 → 只读采集 → 闸门上报）
**在完全离线的情况下用真浏览器跑一遍**。

它不替代真机联调——它只是把真机联调时的变量收敛到"选择器对不对"这一个。
"""

from __future__ import annotations

import contextlib
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

# fixtures/ 在仓库根目录（src/amz_harness/mockserver.py -> parents[2]）。
# 也可用环境变量 AMZ_MOCK_FIXTURES 覆盖。
DEFAULT_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "mock_sellercentral"

ROUTES: dict[str, str] = {
    "/": "dashboard.html",
    "/home": "dashboard.html",
    "/ap/signin": "signin.html",
    "/ap/mfa": "mfa.html",
    "/ap/captcha": "captcha.html",
    "/errors/validateCaptcha": "captcha.html",
    "/ap/sessionexpired": "session_expired.html",
    "/orders-v3/order/": "orders.html",
    "/inventory/": "inventory.html",
}


def fixtures_dir() -> Path:
    return Path(os.environ.get("AMZ_MOCK_FIXTURES", DEFAULT_FIXTURES))


# 1x1 透明 PNG，只为让 <img src="/captcha.png"> 别报 404
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100fbd1a24a0000000049454e44ae426082"
)


class _Handler(BaseHTTPRequestHandler):
    directory: Path = DEFAULT_FIXTURES
    server_version = "MockSellerCentral/0.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的既定接口
        path = urlsplit(self.path).path
        if path == "/captcha.png":
            self._send_bytes(_TINY_PNG, "image/png")
            return
        name = ROUTES.get(path)
        if name is None:
            self._send_bytes(
                f"<h1>404</h1><p>mock 没有配置路由：{path}</p>".encode(),
                "text/html; charset=utf-8",
                status=404,
            )
            return
        targets = [self.directory / name, DEFAULT_FIXTURES / name]
        for target in targets:
            if target.exists():
                self._send_bytes(target.read_bytes(), "text/html; charset=utf-8")
                return
        self._send_bytes(
            "<h1>500</h1><p>夹具缺失</p>".encode(), "text/html; charset=utf-8", status=500
        )

    do_POST = do_GET

    def _send_bytes(self, body: bytes, content_type: str, *, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # 静音，测试输出才干净
        return


@contextlib.contextmanager
def mock_seller_central(directory: Path | None = None, *, port: int = 0):
    """起一个 mock 后台，yield 出 base_url（如 http://127.0.0.1:53412），退出时关掉。"""
    handler = type("_BoundHandler", (_Handler,), {"directory": directory or fixtures_dir()})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, name="mock-sellercentral", daemon=True)
    thread.start()
    host, bound_port = server.server_address[0], server.server_address[1]
    try:
        yield f"http://{host}:{bound_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="本地 mock 卖家后台")
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--dir", type=Path, default=None)
    args = parser.parse_args(argv)
    with mock_seller_central(args.dir, port=args.port) as base_url:
        print(f"mock 卖家后台已启动：{base_url}")
        for route, name in ROUTES.items():
            print(f"  {base_url}{route:<26} -> {name}")
        print("Ctrl+C 退出")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print("\n已停止")
    return 0
