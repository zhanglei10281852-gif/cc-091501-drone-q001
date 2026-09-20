"""HTTP 层:路由、JSON 错误格式与完整放行流程。"""
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402
from app import create_server  # noqa: E402


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server({"db_path": ":memory:", "scheduler": False, "port": 0, "host": "127.0.0.1"})
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.server.shutdown)
        cls.addClassCleanup(cls.server.server_close)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    def request(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_full_flow_over_http(self):
        status, _ = self.request("PUT", "/aircraft-models/m300", {
            "max_altitude_m": 500, "max_range_m": 50000, "endurance_minutes": 240,
            "max_speed_mps": 25, "supports_hover": True,
        })
        self.assertEqual(status, 200)
        status, _ = self.request("PUT", "/operators/op-01", {
            "license_valid_until": "2027-06-01T00:00:00+08:00",
            "categories": ["multirotor"], "endorsements": ["survey"],
        })
        self.assertEqual(status, 200)
        status, zone = self.request("PUT", "/zones/Z1", {
            "version": 1, "kind": "no_fly", "name": "禁飞区",
            "geometry": helpers.square(116.0, 39.0, 116.01, 39.01),
            "floor_m": 0, "ceiling_m": 300,
        })
        self.assertEqual(status, 201)
        self.assertEqual(zone["zone_id"], "Z1")

        status, res = self.request("POST", "/missions", {
            "mission_id": "M-HTTP", "idempotency_key": "http-key-1",
            "plan": helpers.make_plan(segments=[
                {"segment_id": "to", "phase": "takeoff",
                 "geometry": {"type": "Point", "coordinates": [116.005, 39.005]},
                 "start": "2026-09-20T18:00:00+08:00", "end": "2026-09-20T18:05:00+08:00",
                 "altitude_m": 50},
            ]),
        })
        self.assertEqual(status, 201)
        self.assertEqual(res["verdict"], "REJECTED")

        # 幂等重放:同键再提交返回同一决定且不再产生新版本
        status, replay = self.request("POST", "/missions", {
            "mission_id": "M-HTTP", "idempotency_key": "http-key-1",
            "plan": helpers.make_plan(),
        })
        self.assertEqual(status, 200)
        self.assertEqual(replay["decision_id"], res["decision_id"])
        self.assertTrue(replay["replayed"])

        status, trace = self.request("GET", f"/decisions/{res['decision_id']}/trace")
        self.assertEqual(status, 200)
        self.assertEqual(trace["snapshot"]["zones"][0]["zone_id"], "Z1")

        status, mission = self.request("GET", "/missions/M-HTTP")
        self.assertEqual(status, 200)
        self.assertEqual(len(mission["plans"]), 1)

    def test_error_format_and_status_codes(self):
        status, body = self.request("GET", "/decisions/dec-missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "decision_not_found")

        status, body = self.request("POST", "/missions", {"mission_id": "X"})
        self.assertEqual(status, 422)
        self.assertIn("error", body)

        status, body = self.request("PUT", "/zones/ZBAD", {"version": 1, "kind": "unknown", "geometry": {}})
        self.assertEqual(status, 422)

        status, body = self.request("GET", "/no-such-route")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_run_due_jobs_endpoint(self):
        status, body = self.request("POST", "/jobs/run-due", {})
        self.assertEqual(status, 200)
        self.assertIn("jobs_done", body)


if __name__ == "__main__":
    unittest.main()
