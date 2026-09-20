import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from release.service import (
    ConflictError,
    NotFoundError,
    ReleaseService,
    ServiceConfig,
    ValidationError,
)

SERVICE_NAME = "drone-management-starter"


def _routes(service):
    return [
        ("GET", re.compile(r"^/health$"), lambda m, q, b: (200, {"status": "ok", "service": SERVICE_NAME})),
        ("POST", re.compile(r"^/airspaces$"), lambda m, q, b: (201, service.add_airspace(b))),
        ("GET", re.compile(r"^/airspaces$"), lambda m, q, b: (200, {"airspaces": service.list_airspaces()})),
        ("GET", re.compile(r"^/airspaces/([^/]+)$"), lambda m, q, b: (200, {"versions": service.airspace_history(m.group(1))})),
        ("POST", re.compile(r"^/notices$"), lambda m, q, b: (201, service.publish_notice(b))),
        ("GET", re.compile(r"^/notices$"), lambda m, q, b: (200, {"notices": service.list_notices()})),
        ("GET", re.compile(r"^/notices/([^/]+)$"), lambda m, q, b: (200, {"versions": service.notice_history(m.group(1))})),
        ("POST", re.compile(r"^/missions$"), lambda m, q, b: (201, service.submit_mission(b))),
        ("GET", re.compile(r"^/missions$"), lambda m, q, b: (200, {"missions": service.list_missions()})),
        ("GET", re.compile(r"^/missions/([^/]+)$"), lambda m, q, b: (200, service.get_mission(m.group(1)))),
        ("POST", re.compile(r"^/missions/([^/]+)/plans$"), lambda m, q, b: (201, service.submit_plan(m.group(1), b))),
        ("POST", re.compile(r"^/missions/([^/]+)/evaluate$"), lambda m, q, b: (201, service.evaluate_mission(m.group(1), b))),
        ("GET", re.compile(r"^/missions/([^/]+)/decisions$"), lambda m, q, b: (200, {"decisions": service.list_decisions(m.group(1))})),
        ("GET", re.compile(r"^/decisions/([^/]+)$"), lambda m, q, b: (200, service.get_decision(m.group(1)))),
        ("GET", re.compile(r"^/reviews$"), lambda m, q, b: (200, {"reviews": service.list_reviews(_query_one(q, "status"))})),
        ("POST", re.compile(r"^/reviews/([^/]+)/resolve$"), lambda m, q, b: (200, service.resolve_review(m.group(1), b))),
        ("GET", re.compile(r"^/reminders$"), lambda m, q, b: (200, {"reminders": service.list_reminders(_query_one(q, "status"))})),
    ]


def _query_one(query, name):
    values = query.get(name)
    return values[0] if values else None


class Handler(BaseHTTPRequestHandler):
    service = None  # 由 create_server 注入
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        body = None
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send(400, {"error": "bad_request", "message": "请求体不是合法 JSON"})
                    return
        for route_method, pattern, handler in _routes(self.service):
            if route_method != method:
                continue
            match = pattern.match(parsed.path)
            if not match:
                continue
            try:
                status, payload = handler(match, query, body)
            except ValidationError as exc:
                self._send(422, {"error": "validation", "message": str(exc)})
            except NotFoundError as exc:
                self._send(404, {"error": "not_found", "message": str(exc)})
            except ConflictError as exc:
                self._send(409, {"error": "conflict", "message": str(exc)})
            except BrokenPipeError:
                raise
            except Exception as exc:  # 兜底，避免连接悬挂
                self._send(500, {"error": "internal", "message": f"{type(exc).__name__}: {exc}"})
            else:
                self._send(status, payload)
            return
        self._send(404, {"error": "not_found", "message": f"无此路由: {method} {parsed.path}"})

    def _send(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        return


def create_server(config=None):
    cfg = config or ServiceConfig.from_env()
    service = ReleaseService(cfg)

    class BoundHandler(Handler):
        pass

    BoundHandler.service = service
    server = ThreadingHTTPServer((cfg.host, cfg.port), BoundHandler)
    server.service = service
    return server
