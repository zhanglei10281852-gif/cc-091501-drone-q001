"""HTTP 路由:把 JSON 请求映射到 ClearanceService。

统一错误格式 {"error": {"code", "message"}};业务错误由 ApiError 携带状态码。
"""
from __future__ import annotations

import json
import re
from urllib.parse import unquote, urlparse, parse_qs

from .service import ApiError, ClearanceService


class ClearanceAPI:
    def __init__(self, service: ClearanceService):
        self.service = service

    # method, 路径模式(花括号占位), 处理函数
    _ROUTES = [
        ("PUT", "/zones/{zone_id}", "put_zone"),
        ("GET", "/zones", "list_zones"),
        ("GET", "/zones/{zone_id}", "get_zone"),
        ("PUT", "/notices/{notice_id}", "put_notice"),
        ("GET", "/notices", "list_notices"),
        ("POST", "/notices/{notice_id}/withdraw", "withdraw_notice"),
        ("PUT", "/aircraft-models/{model_id}", "put_aircraft_model"),
        ("GET", "/aircraft-models/{model_id}", "get_aircraft_model"),
        ("PUT", "/operators/{operator_id}", "put_operator"),
        ("GET", "/operators/{operator_id}", "get_operator"),
        ("PUT", "/rulesets/{version}", "put_ruleset"),
        ("GET", "/rulesets/current", "get_current_ruleset"),
        ("POST", "/missions", "submit_mission"),
        ("GET", "/missions/{mission_id}", "get_mission"),
        ("GET", "/decisions/{decision_id}", "get_decision"),
        ("GET", "/decisions/{decision_id}/trace", "get_trace"),
        ("POST", "/decisions/{decision_id}/annotations", "add_annotation"),
        ("GET", "/reviews", "list_reviews"),
        ("POST", "/reviews/{review_id}/ack", "ack_review"),
        ("POST", "/reviews/{review_id}/resolve", "resolve_review"),
        ("GET", "/reminders", "list_reminders"),
        ("POST", "/jobs/run-due", "run_due_jobs"),
    ]

    def handle(self, method, raw_path, body):
        parsed = urlparse(raw_path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        for route_method, pattern, handler_name in self._ROUTES:
            if route_method != method:
                continue
            params = self._match(pattern, path)
            if params is None:
                continue
            handler = getattr(self, f"_{handler_name}")
            return handler(params, query, body or {})
        raise ApiError(404, "not_found", f"无此路由: {method} {path}")

    @staticmethod
    def _match(pattern, path):
        p_parts = [p for p in pattern.split("/") if p]
        parts = [p for p in path.split("/") if p]
        if len(p_parts) != len(parts):
            return None
        params = {}
        for want, got in zip(p_parts, parts):
            if want.startswith("{") and want.endswith("}"):
                params[want[1:-1]] = unquote(got)
            elif want != got:
                return None
        return params

    @staticmethod
    def _actor(body):
        return body.get("actor") or "system"

    # ------------------------------------------------------------ 各端点

    def _put_zone(self, params, query, body):
        return 201, self.service.put_zone(params["zone_id"], body, actor=self._actor(body))

    def _list_zones(self, params, query, body):
        return 200, {"zones": self.service.list_zones()}

    def _get_zone(self, params, query, body):
        for zone in self.service.list_zones():
            if zone["zone_id"] == params["zone_id"]:
                return 200, zone
        raise ApiError(404, "zone_not_found", f"空域 {params['zone_id']} 不存在")

    def _put_notice(self, params, query, body):
        return 201, self.service.put_notice(params["notice_id"], body, actor=self._actor(body))

    def _list_notices(self, params, query, body):
        return 200, {"notices": self.service.list_notices()}

    def _withdraw_notice(self, params, query, body):
        return 200, self.service.withdraw_notice(
            params["notice_id"], actor=self._actor(body),
            reason=body.get("reason"), withdrawn_at=body.get("withdrawn_at"),
        )

    def _put_aircraft_model(self, params, query, body):
        return 200, self.service.put_aircraft_model(params["model_id"], body, actor=self._actor(body))

    def _get_aircraft_model(self, params, query, body):
        model = self.service._get_reference("aircraft_models", "model_id", params["model_id"])
        if model is None:
            raise ApiError(404, "model_not_found", f"机型 {params['model_id']} 未登记")
        return 200, {"model_id": params["model_id"], "payload": model}

    def _put_operator(self, params, query, body):
        return 200, self.service.put_operator(params["operator_id"], body, actor=self._actor(body))

    def _get_operator(self, params, query, body):
        operator = self.service._get_reference("operators", "operator_id", params["operator_id"])
        if operator is None:
            raise ApiError(404, "operator_not_found", f"操作员 {params['operator_id']} 未登记")
        return 200, {"operator_id": params["operator_id"], "payload": operator}

    def _put_ruleset(self, params, query, body):
        return 201, self.service.put_ruleset(params["version"], body.get("params") or {}, actor=self._actor(body))

    def _get_current_ruleset(self, params, query, body):
        return 200, self.service.current_ruleset()

    def _submit_mission(self, params, query, body):
        result = self.service.submit_mission(body, actor=self._actor(body))
        return (200 if result.get("replayed") else 201), result

    def _get_mission(self, params, query, body):
        return 200, self.service.get_mission(params["mission_id"])

    def _get_decision(self, params, query, body):
        return 200, self.service.get_decision(params["decision_id"])

    def _get_trace(self, params, query, body):
        return 200, self.service.get_trace(params["decision_id"])

    def _add_annotation(self, params, query, body):
        return 201, self.service.add_annotation(
            params["decision_id"], actor=self._actor(body), note=body.get("note")
        )

    def _list_reviews(self, params, query, body):
        status = query.get("status", [None])[0]
        return 200, {"reviews": self.service.list_reviews(status=status)}

    def _ack_review(self, params, query, body):
        return 200, self.service.ack_review(params["review_id"], actor=self._actor(body), note=body.get("note"))

    def _resolve_review(self, params, query, body):
        return 200, self.service.resolve_review(params["review_id"], actor=self._actor(body), note=body.get("note"))

    def _list_reminders(self, params, query, body):
        status = query.get("status", ["pending"])[0]
        return 200, {"reminders": self.service.list_reminders(status=status)}

    def _run_due_jobs(self, params, query, body):
        from .timeutil import parse_instant

        now = body.get("now")
        ran = self.service.run_due_jobs(now=parse_instant(now).timestamp() if now else None)
        return 200, {"jobs_done": ran}


def error_payload(code, message):
    return {"error": {"code": code, "message": message}}


def encode(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")
