import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app import SERVICE_NAME, create_server  # noqa: E402
from release.service import ServiceConfig  # noqa: E402


def square_geo(lon0, lat0, lon1, lat1):
    return {
        "type": "Polygon",
        "coordinates": [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]],
    }


PLAN = {
    "category": "survey",
    "aircraft": {"model": "M350", "max_altitude_m": 500, "max_range_m": 100000,
                 "max_flight_time_s": 7200, "hover_capable": True},
    "operator": {"id": "op1", "categories": ["survey"], "valid_until": None},
    "segments": [
        {"id": "takeoff", "type": "takeoff", "points": [
            {"lon": 121.0002, "lat": 31.0005, "alt_m": 0, "time": "2026-09-20T17:30:00+08:00"},
            {"lon": 121.0002, "lat": 31.0005, "alt_m": 80, "time": "2026-09-20T17:31:00+08:00"},
        ]},
        {"id": "enroute", "type": "enroute", "points": [
            {"lon": 121.0002, "lat": 31.0005, "alt_m": 80, "time": "2026-09-20T17:31:00+08:00"},
            {"lon": 121.0008, "lat": 31.0005, "alt_m": 80, "time": "2026-09-20T18:30:00+08:00"},
        ]},
    ],
}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        config = ServiceConfig(data_dir=cls.tmp.name, host="127.0.0.1", port=0,
                               scheduler_enabled=False)
        cls.server = create_server(config)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.service.close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_full_release_flow(self):
        # 健康检查保持基线行为
        status, body = self.call("GET", "/health")
        self.assertEqual((status, body), (200, {"status": "ok", "service": SERVICE_NAME}))

        # 登记空域（与计划不相交）
        status, body = self.call("POST", "/airspaces", {
            "id": "Z-FAR", "kind": "no_fly", "geometry": square_geo(120.5, 31.5, 120.6, 31.6),
        })
        self.assertEqual(status, 201)
        self.assertEqual(body["version"], 1)

        # 提交任务：批准，分段审查齐全
        status, body = self.call("POST", "/missions", {
            "mission_id": "API-M1", "plan": PLAN, "decided_by": "放行员张三",
        })
        self.assertEqual(status, 201)
        decision = body["decision"]
        self.assertEqual(decision["verdict"], "approved")
        self.assertEqual({s["label"] for s in decision["segments"]}, {"起飞", "航路"})

        # 相同任务重复提交：409
        status, body = self.call("POST", "/missions", {"mission_id": "API-M1", "plan": PLAN})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "conflict")

        # 发布立即生效的禁飞通告，覆盖计划区域：生成待复核与提醒，不改写原决策
        # effective_from 取当天凌晨，避免测试依赖真实时钟
        status, body = self.call("POST", "/notices", {
            "id": "NOTAM-API", "op": "publish", "kind": "no_fly",
            "geometry": square_geo(121.000, 31.000, 121.001, 31.001),
            "effective_from": "2026-09-20T00:00:00+08:00",
            "effective_until": "2026-09-20T23:30:00+08:00",
            "reason": "大型活动",
        })
        self.assertEqual(status, 201)
        self.assertEqual(body["affected"], 1)

        status, body = self.call("GET", "/reviews?status=pending")
        self.assertEqual(len(body["reviews"]), 1)
        review = body["reviews"][0]
        self.assertEqual(review["mission_id"], "API-M1")

        status, body = self.call("GET", "/reminders?status=open")
        self.assertEqual(len(body["reminders"]), 1)

        # 原决策仍是批准，且可追溯完整判断过程
        status, fetched = self.call("GET", f"/decisions/{decision['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["verdict"], "approved")
        self.assertEqual(fetched["snapshot"]["ruleset_version"], "release-rules/1.0.0")
        self.assertEqual(fetched["snapshot"]["airspace"][0]["id"], "Z-FAR")

        # 复核并重新评估：新决策拒绝（通告生效中），历史保留
        status, body = self.call("POST", f"/reviews/{review['id']}/resolve", {
            "resolved_by": "放行员李四", "note": "通告生效", "re_evaluate": True,
        })
        self.assertEqual(status, 200)
        new_id = body["resolution"]["new_decision_id"]
        status, new_decision = self.call("GET", f"/decisions/{new_id}")
        self.assertEqual(new_decision["verdict"], "rejected")
        self.assertEqual(new_decision["snapshot"]["notices"],
                         [{"id": "NOTAM-API", "version": 1}])

        status, body = self.call("GET", "/missions/API-M1/decisions")
        self.assertEqual(len(body["decisions"]), 2)

        # 撤回通告后再评估：批准
        status, body = self.call("POST", "/notices", {"id": "NOTAM-API", "op": "withdraw"})
        self.assertEqual(body["version"], 2)
        status, body = self.call("POST", "/missions/API-M1/evaluate", {"decided_by": "值班员"})
        self.assertEqual(body["verdict"], "approved")

    def test_error_responses(self):
        status, body = self.call("GET", "/nope")
        self.assertEqual(status, 404)
        status, body = self.call("POST", "/missions", {"mission_id": "BAD", "plan": {
            "category": "survey", "aircraft": {"model": "x"}, "operator": {"id": "o"},
            "segments": [{"type": "takeoff", "points": [
                {"lon": 1, "lat": 1, "alt_m": 0, "time": "2026-09-20 10:00:00"},
            ]}],
        }})
        self.assertEqual(status, 422)  # 时间缺时区
        status, body = self.call("GET", "/decisions/dec-nonexistent")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
