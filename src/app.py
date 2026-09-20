import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from clearance.api import ClearanceAPI, encode, error_payload
from clearance.scheduler import Scheduler
from clearance.service import ApiError, ClearanceService
from clearance.store import Store

SERVICE_NAME = "drone-management-starter"


class ClearanceServer(ThreadingHTTPServer):
    """挂载业务服务与调度器的 HTTP 服务。"""

    daemon_threads = True

    def __init__(self, address, handler, service, scheduler):
        super().__init__(address, handler)
        self.service = service
        self.scheduler = scheduler

    def server_close(self):
        if self.scheduler is not None:
            self.scheduler.stop()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self):  # noqa: N802
        self._dispatch("PUT")

    def _dispatch(self, method):
        if self.path == "/health" and method == "GET":
            return self._respond(200, {"status": "ok", "service": SERVICE_NAME})
        body = None
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._respond(400, error_payload("invalid_json", "请求体不是合法 JSON"))
        try:
            status, payload = self.server.api.handle(method, self.path, body)
        except ApiError as exc:
            return self._respond(exc.status, error_payload(exc.code, exc.message))
        except Exception as exc:  # 未预期错误:记录并返回 500,不泄露堆栈
            import traceback

            traceback.print_exc()
            return self._respond(500, error_payload("internal_error", str(exc)))
        self._respond(status, payload)

    def _respond(self, status, payload):
        data = encode(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        return


def create_server(config=None):
    """装配服务。config 可覆盖:db_path / clock / scheduler(bool) / poll_interval。

    数据库路径由运行时配置(环境变量 DRONE_OPS_DB 或 config)指定;
    缺省 ":memory:" 不落盘,测试不依赖主机隐藏状态。
    """
    config = dict(config or {})
    db_path = config.get("db_path") or os.environ.get("DRONE_OPS_DB") or ":memory:"
    store = Store(db_path)
    service = ClearanceService(store, clock=config.get("clock") or time.time)
    service.recover()  # 重启续跑:立即补跑到期的定时工作
    scheduler = None
    if config.get("scheduler", True):
        scheduler = Scheduler(service, poll_interval=float(config.get("poll_interval", 1.0)))
        scheduler.start()
    port = config.get("port")
    if port is None:
        port = int(os.environ.get("PORT", "8000"))
    host = config.get("host")
    if host is None:
        host = os.environ.get("HOST", "0.0.0.0")
    server = ClearanceServer((host, port), Handler, service, scheduler)
    server.api = ClearanceAPI(service)
    return server
