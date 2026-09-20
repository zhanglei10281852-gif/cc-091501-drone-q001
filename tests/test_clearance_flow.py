"""端到端:三支测绘任务横跨通告生效时刻的放行判断与追溯。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402


class ClearanceFlowTest(unittest.TestCase):
    def setUp(self):
        self.svc = helpers.make_service()
        helpers.register_basics(self.svc)
        # 常态化禁飞区
        self.svc.put_zone(
            "Z-PARK",
            {
                "version": 1, "kind": "no_fly", "name": "中心公园禁飞区",
                "geometry": helpers.square(116.0, 39.0, 116.01, 39.01),
                "floor_m": 0, "ceiling_m": 300,
            },
        )
        # 大型活动临时禁飞通告:20:00 生效,23:00 解除
        self.svc.put_notice(
            "N-EVENT",
            {
                "version": 1, "kind": "no_fly", "title": "大型活动临时禁飞",
                "geometry": helpers.square(116.02, 39.0, 116.03, 39.01),
                "effective_from": "2026-09-20T20:00:00+08:00",
                "effective_to": "2026-09-20T23:00:00+08:00",
                "reason": "开场活动",
            },
        )

    def test_mission_before_notice_window_is_approved(self):
        """任务完全在通告生效前 → 批准。"""
        plan = helpers.make_plan(segments=[
            {"segment_id": "to", "phase": "takeoff",
             "geometry": {"type": "Point", "coordinates": [116.025, 39.005]},
             "start": "2026-09-20T18:30:00+08:00", "end": "2026-09-20T18:35:00+08:00", "altitude_m": 50},
            {"segment_id": "enr", "phase": "enroute",
             "geometry": {"type": "LineString", "coordinates": [[116.025, 39.005], [116.028, 39.008]]},
             "start": "2026-09-20T18:35:00+08:00", "end": "2026-09-20T19:30:00+08:00", "altitude_m": 120},
        ])
        res = helpers.submit(self.svc, "M-EARLY", plan=plan)
        self.assertEqual(res["verdict"], "APPROVED")
        self.assertTrue(all(sr["verdict"] == "CLEAR" for sr in res["result"]["segment_results"]))

    def test_mission_spanning_notice_activation_is_rejected_per_segment(self):
        """横跨通告生效时刻:生效后的航路段判违规,生效前的段不受影响。"""
        plan = helpers.make_plan(segments=[
            {"segment_id": "to", "phase": "takeoff",
             "geometry": {"type": "Point", "coordinates": [116.05, 39.005]},
             "start": "2026-09-20T19:00:00+08:00", "end": "2026-09-20T19:05:00+08:00", "altitude_m": 50},
            {"segment_id": "enr", "phase": "enroute",
             "geometry": {"type": "LineString", "coordinates": [[116.05, 39.005], [116.025, 39.005]]},
             "start": "2026-09-20T19:05:00+08:00", "end": "2026-09-20T21:00:00+08:00", "altitude_m": 120},
            {"segment_id": "alt", "phase": "alternate",
             "geometry": {"type": "LineString", "coordinates": [[116.025, 39.005], [116.06, 39.005]]},
             "start": "2026-09-20T21:00:00+08:00", "end": "2026-09-20T21:30:00+08:00", "altitude_m": 100},
        ])
        res = helpers.submit(self.svc, "M-SPAN", plan=plan)
        self.assertEqual(res["verdict"], "REJECTED")
        by_id = {sr["segment_id"]: sr for sr in res["result"]["segment_results"]}
        self.assertEqual(by_id["to"]["verdict"], "CLEAR")  # 生效前起飞,不追溯
        self.assertEqual(by_id["enr"]["verdict"], "VIOLATION")
        codes = [r["code"] for r in by_id["enr"]["reasons"]]
        self.assertIn("NOTICE_NO_FLY", codes)
        # 备降段同样被审查:21:00 后通告仍生效,且起点在通告区内
        self.assertEqual(by_id["alt"]["verdict"], "VIOLATION")

    def test_near_no_fly_zone_is_conditionally_approved(self):
        """贴近但未进入禁飞区 → 附条件批准,条件含保持间隔。"""
        plan = helpers.make_plan(segments=[
            {"segment_id": "to", "phase": "takeoff",
             "geometry": {"type": "Point", "coordinates": [116.0115, 39.005]},
             "start": "2026-09-20T18:00:00+08:00", "end": "2026-09-20T18:05:00+08:00", "altitude_m": 50},
            {"segment_id": "enr", "phase": "enroute",
             "geometry": {"type": "LineString", "coordinates": [[116.0115, 39.005], [116.0115, 39.02]]},
             "start": "2026-09-20T18:05:00+08:00", "end": "2026-09-20T18:30:00+08:00", "altitude_m": 120},
        ])
        res = helpers.submit(self.svc, "M-NEAR", plan=plan)
        self.assertEqual(res["verdict"], "CONDITIONALLY_APPROVED")
        self.assertTrue(any("保持至少" in c for c in res["result"]["conditions"]))
        buffer_reasons = [
            r for sr in res["result"]["segment_results"] for r in sr["reasons"]
            if r["code"] == "AIRSPACE_BUFFER"
        ]
        self.assertTrue(buffer_reasons)
        self.assertEqual(buffer_reasons[0]["zone_id"], "Z-PARK")

    def test_capability_and_qualification_checks(self):
        """机型能力与操作员资质进入同一次判断。"""
        # 悬停段超出机型升限
        plan = helpers.make_plan(segments=[
            {"segment_id": "hov", "phase": "hover",
             "geometry": {"type": "Point", "coordinates": [117.0, 39.0]},
             "start": "2026-09-20T18:00:00+08:00", "end": "2026-09-20T18:10:00+08:00", "altitude_m": 600},
        ])
        res = helpers.submit(self.svc, "M-ALT", plan=plan)
        self.assertEqual(res["verdict"], "REJECTED")
        codes = [r["code"] for sr in res["result"]["segment_results"] for r in sr["reasons"]]
        self.assertIn("CAP_ALTITUDE", codes)

        # 操作员缺少签注
        plan2 = helpers.make_plan(required_endorsements=["survey", "over_people"])
        res2 = helpers.submit(self.svc, "M-ENDORSE", plan=plan2)
        self.assertEqual(res2["verdict"], "REJECTED")
        codes2 = [r["code"] for r in res2["result"]["mission_reasons"]]
        self.assertIn("OP_ENDORSEMENT", codes2)

    def test_trace_freezes_inputs_and_human_notes(self):
        """任一结论都可追溯:快照固化空域/通告/规则/机型/资质/人工意见。"""
        res = helpers.submit(
            self.svc, "M-TRACE",
            human_notes=[{"actor": "dispatcher-1", "note": "已与活动方确认"}],
        )
        self.svc.add_annotation(res["decision_id"], actor="chief", note="值班主任复核同意")
        trace = self.svc.get_trace(res["decision_id"])
        self.assertEqual(trace["decision_id"], res["decision_id"])
        snapshot = trace["snapshot"]
        self.assertEqual([(z["zone_id"], z["version"]) for z in snapshot["zones"]], [("Z-PARK", 1)])
        self.assertEqual([(n["notice_id"], n["version"]) for n in snapshot["notices"]], [("N-EVENT", 1)])
        self.assertEqual(snapshot["ruleset"]["version"], "builtin-v1")
        self.assertEqual(snapshot["aircraft_model"]["model_id"], "m300")
        self.assertEqual(snapshot["operator"]["operator_id"], "op-01")
        self.assertEqual(snapshot["human_notes"][0]["note"], "已与活动方确认")
        self.assertEqual(trace["annotations"][0]["note"], "值班主任复核同意")
        actions = [e["action"] for e in trace["audit_events"]]
        self.assertIn("mission.submit", actions)
        self.assertIn("decision.annotate", actions)

    def test_snapshot_is_immutable_after_zone_update(self):
        """空域发新版本后,旧决定的快照仍固化当时的版本。"""
        res = helpers.submit(self.svc, "M-SNAPSHOT")
        self.svc.put_zone(
            "Z-PARK",
            {
                "version": 2, "kind": "no_fly", "name": "中心公园禁飞区(扩大)",
                "geometry": helpers.square(116.0, 39.0, 116.02, 39.02),
                "floor_m": 0, "ceiling_m": 500,
            },
        )
        trace = self.svc.get_trace(res["decision_id"])
        zone_versions = [(z["zone_id"], z["version"]) for z in trace["snapshot"]["zones"]]
        self.assertEqual(zone_versions, [("Z-PARK", 1)])
        # 新评估则使用新版本
        res2 = helpers.submit(self.svc, "M-SNAPSHOT-2")
        trace2 = self.svc.get_trace(res2["decision_id"])
        self.assertEqual([(z["zone_id"], z["version"]) for z in trace2["snapshot"]["zones"]], [("Z-PARK", 2)])


if __name__ == "__main__":
    unittest.main()
