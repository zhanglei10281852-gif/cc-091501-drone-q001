"""边界情形稳定规则:相切、跨午夜、半开时刻、通告撤回、新通告影响已放行计划。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402
from clearance.service import ApiError  # noqa: E402
from clearance.timeutil import parse_instant  # noqa: E402


class TangencyTest(unittest.TestCase):
    """边界相切:闭集规则,接触即冲突。"""

    def setUp(self):
        self.svc = helpers.make_service()
        helpers.register_basics(self.svc)
        self.svc.put_zone(
            "Z1",
            {
                "version": 1, "kind": "no_fly", "name": "禁飞区",
                "geometry": helpers.square(116.0, 39.0, 116.01, 39.01),
                "floor_m": 0, "ceiling_m": 300,
            },
        )

    def _enroute_plan(self, coords):
        return helpers.make_plan(segments=[
            {"segment_id": "enr", "phase": "enroute",
             "geometry": {"type": "LineString", "coordinates": coords},
             "start": "2026-09-20T18:00:00+08:00", "end": "2026-09-20T18:30:00+08:00", "altitude_m": 120},
        ])

    def test_tangent_path_is_rejected(self):
        # 航线沿 x=116.01 边界擦过(相切)
        plan = self._enroute_plan([[116.01, 38.98], [116.01, 39.02]])
        res = helpers.submit(self.svc, "M-TANGENT", plan=plan)
        self.assertEqual(res["verdict"], "REJECTED")
        codes = [r["code"] for sr in res["result"]["segment_results"] for r in sr["reasons"]]
        self.assertIn("AIRSPACE_NO_FLY", codes)

    def test_path_along_boundary_is_rejected(self):
        plan = self._enroute_plan([[115.998, 39.0], [116.012, 39.0]])
        res = helpers.submit(self.svc, "M-BOUNDARY", plan=plan)
        self.assertEqual(res["verdict"], "REJECTED")

    def test_takeoff_on_vertex_is_rejected(self):
        plan = helpers.make_plan(segments=[
            {"segment_id": "to", "phase": "takeoff",
             "geometry": {"type": "Point", "coordinates": [116.01, 39.01]},
             "start": "2026-09-20T18:00:00+08:00", "end": "2026-09-20T18:05:00+08:00", "altitude_m": 30},
        ])
        res = helpers.submit(self.svc, "M-VERTEX", plan=plan)
        self.assertEqual(res["verdict"], "REJECTED")

    def test_altitude_exactly_at_ceiling_conflicts(self):
        # 高度闭区间:恰好等于上限也算冲突
        plan = helpers.make_plan(segments=[
            {"segment_id": "to", "phase": "takeoff",
             "geometry": {"type": "Point", "coordinates": [116.005, 39.005]},
             "start": "2026-09-20T18:00:00+08:00", "end": "2026-09-20T18:05:00+08:00", "altitude_m": 300},
        ])
        res = helpers.submit(self.svc, "M-CEIL", plan=plan)
        self.assertEqual(res["verdict"], "REJECTED")


class MidnightCrossingTest(unittest.TestCase):
    """跨午夜:每日窗口与半开时刻边界。"""

    def setUp(self):
        self.svc = helpers.make_service()
        helpers.register_basics(self.svc)
        self.svc.put_zone(
            "Z-NIGHT",
            {
                "version": 1, "kind": "no_fly", "name": "夜间限飞区",
                "geometry": helpers.square(116.0, 39.0, 116.01, 39.01),
                "floor_m": 0, "ceiling_m": 500,
                "daily_windows": [{"start": "22:00", "end": "06:00"}],
            },
        )

    def _hover_plan(self, start, end):
        return helpers.make_plan(segments=[
            {"segment_id": "hov", "phase": "hover",
             "geometry": {"type": "Point", "coordinates": [116.005, 39.005]},
             "start": start, "end": end, "altitude_m": 100},
        ])

    def test_segment_crossing_midnight_conflicts(self):
        plan = self._hover_plan("2026-09-20T23:30:00+08:00", "2026-09-21T00:30:00+08:00")
        res = helpers.submit(self.svc, "M-NIGHT", plan=plan)
        self.assertEqual(res["verdict"], "REJECTED")

    def test_segment_ending_exactly_at_window_start_is_clear(self):
        # 半开区间:段结束时刻 == 窗口开始时刻 → 不冲突
        plan = self._hover_plan("2026-09-20T21:00:00+08:00", "2026-09-20T22:00:00+08:00")
        res = helpers.submit(self.svc, "M-BEFORE-WINDOW", plan=plan)
        self.assertEqual(res["verdict"], "APPROVED")

    def test_segment_starting_exactly_at_window_end_is_clear(self):
        # 半开区间:段开始时刻 == 窗口结束时刻 → 不冲突
        plan = self._hover_plan("2026-09-21T06:00:00+08:00", "2026-09-21T07:00:00+08:00")
        res = helpers.submit(self.svc, "M-AFTER-WINDOW", plan=plan)
        self.assertEqual(res["verdict"], "APPROVED")

    def test_daytime_segment_is_clear(self):
        plan = self._hover_plan("2026-09-20T12:00:00+08:00", "2026-09-20T13:00:00+08:00")
        res = helpers.submit(self.svc, "M-DAY", plan=plan)
        self.assertEqual(res["verdict"], "APPROVED")


class NoticeBoundaryTest(unittest.TestCase):
    """通告生效时刻的半开规则。"""

    def setUp(self):
        self.svc = helpers.make_service()
        helpers.register_basics(self.svc)
        self.svc.put_notice(
            "N1",
            {
                "version": 1, "kind": "no_fly", "title": "临时禁飞",
                "geometry": helpers.square(116.0, 39.0, 116.01, 39.01),
                "effective_from": "2026-09-20T20:00:00+08:00",
                "effective_to": "2026-09-20T23:00:00+08:00",
            },
        )

    def _hover_plan(self, start, end):
        return helpers.make_plan(segments=[
            {"segment_id": "hov", "phase": "hover",
             "geometry": {"type": "Point", "coordinates": [116.005, 39.005]},
             "start": start, "end": end, "altitude_m": 100},
        ])

    def test_segment_ending_at_effective_from_is_clear(self):
        plan = self._hover_plan("2026-09-20T19:00:00+08:00", "2026-09-20T20:00:00+08:00")
        self.assertEqual(helpers.submit(self.svc, "M-END-AT-START", plan=plan)["verdict"], "APPROVED")

    def test_segment_starting_at_effective_from_conflicts(self):
        plan = self._hover_plan("2026-09-20T20:00:00+08:00", "2026-09-20T21:00:00+08:00")
        self.assertEqual(helpers.submit(self.svc, "M-START-AT-START", plan=plan)["verdict"], "REJECTED")

    def test_segment_starting_at_effective_to_is_clear(self):
        plan = self._hover_plan("2026-09-20T23:00:00+08:00", "2026-09-21T00:00:00+08:00")
        self.assertEqual(helpers.submit(self.svc, "M-START-AT-END", plan=plan)["verdict"], "APPROVED")


class WithdrawalTest(unittest.TestCase):
    """通告撤回:对后续评估立即生效,历史决定不变,生成提醒。"""

    def setUp(self):
        self.svc = helpers.make_service()
        helpers.register_basics(self.svc)
        self.svc.put_notice(
            "N-W",
            {
                "version": 1, "kind": "no_fly", "title": "临时禁飞",
                "geometry": helpers.square(116.0, 39.0, 116.01, 39.01),
                "effective_from": "2026-09-20T18:00:00+08:00",
                "effective_to": "2026-09-20T23:00:00+08:00",
            },
        )
        self.plan = helpers.make_plan(segments=[
            {"segment_id": "hov", "phase": "hover",
             "geometry": {"type": "Point", "coordinates": [116.005, 39.005]},
             "start": "2026-09-20T19:00:00+08:00", "end": "2026-09-20T20:00:00+08:00", "altitude_m": 100},
        ])

    def test_withdrawal_rules(self):
        rejected = helpers.submit(self.svc, "M-W", plan=self.plan)
        self.assertEqual(rejected["verdict"], "REJECTED")

        outcome = self.svc.withdraw_notice("N-W", actor="dispatcher", reason="活动取消")
        self.assertEqual(outcome["reminders_raised"], 1)

        # 原决定保持不变,历史快照仍记录当时引用的通告
        original = self.svc.get_decision(rejected["decision_id"])
        self.assertEqual(original["verdict"], "REJECTED")
        trace = self.svc.get_trace(rejected["decision_id"])
        self.assertEqual([n["notice_id"] for n in trace["snapshot"]["notices"]], ["N-W"])

        # 撤回提醒可见
        reminders = self.svc.list_reminders()
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]["kind"], "notice_withdrawn")
        self.assertEqual(reminders[0]["decision_id"], rejected["decision_id"])

        # 撤回后同一计划重新评估 → 批准(新计划版本)
        resubmitted = helpers.submit(self.svc, "M-W", key="key-M-W-2", plan=self.plan, base=1)
        self.assertEqual(resubmitted["verdict"], "APPROVED")
        self.assertEqual(resubmitted["plan_version"], 2)

        # 已撤回的通告不能发新版本;重复撤回返回 409
        with self.assertRaises(ApiError) as ctx:
            self.svc.put_notice(
                "N-W",
                {
                    "version": 2, "kind": "no_fly", "title": "换个范围",
                    "geometry": helpers.square(116.0, 39.0, 116.02, 39.02),
                    "effective_from": "2026-09-21T18:00:00+08:00",
                },
            )
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(ApiError) as ctx2:
            self.svc.withdraw_notice("N-W", actor="dispatcher")
        self.assertEqual(ctx2.exception.status, 409)


class NewNoticeImpactTest(unittest.TestCase):
    """新禁飞区影响已放行计划:只生成待复核与提醒,不改写原决定。"""

    def setUp(self):
        self.svc = helpers.make_service()
        helpers.register_basics(self.svc)
        # 任务航路经过 (116.005, 39.005),提交时无通告 → 批准
        plan = helpers.make_plan(segments=[
            {"segment_id": "enr", "phase": "enroute",
             "geometry": {"type": "LineString", "coordinates": [[116.0, 39.005], [116.01, 39.005]]},
             "start": "2026-09-20T19:00:00+08:00", "end": "2026-09-20T20:00:00+08:00", "altitude_m": 120},
        ])
        self.approved = helpers.submit(self.svc, "M-RELEASED", plan=plan)
        self.assertEqual(self.approved["verdict"], "APPROVED")

    def test_immediate_notice_raises_review_and_reminder_only(self):
        # 发布即生效(effective_from 早于当前时钟 12:00)的通告
        self.svc.put_notice(
            "N-NEW",
            {
                "version": 1, "kind": "no_fly", "title": "突发禁飞",
                "geometry": helpers.square(116.0, 39.0, 116.01, 39.01),
                "effective_from": "2026-09-20T11:30:00+08:00",
                "effective_to": "2026-09-20T21:00:00+08:00",
            },
        )
        # 原决定一字不动
        decision = self.svc.get_decision(self.approved["decision_id"])
        self.assertEqual(decision["verdict"], "APPROVED")
        # 待复核清单与提醒已生成
        reviews = self.svc.list_reviews(status="pending")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["decision_id"], self.approved["decision_id"])
        self.assertEqual(reviews[0]["notice_id"], "N-NEW")
        reminders = self.svc.list_reminders()
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]["kind"], "review_pending")
        # 复核流程:认领 → 办结
        review_id = reviews[0]["review_id"]
        acked = self.svc.ack_review(review_id, actor="dispatcher-2")
        self.assertEqual(acked["status"], "acknowledged")
        resolved = self.svc.resolve_review(review_id, actor="dispatcher-2", note="已通知任务方改期")
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(self.svc.list_reviews(status="pending"), [])

    def test_scan_is_idempotent(self):
        self.svc.put_notice(
            "N-NEW2",
            {
                "version": 1, "kind": "no_fly", "title": "突发禁飞",
                "geometry": helpers.square(116.0, 39.0, 116.01, 39.01),
                "effective_from": "2026-09-20T11:30:00+08:00",
            },
        )
        self.svc.run_due_jobs()
        self.svc.run_due_jobs()
        self.assertEqual(len(self.svc.list_reviews(status="pending")), 1)
        self.assertEqual(len(self.svc.list_reminders()), 1)

    def test_unaffected_mission_gets_no_review(self):
        far_plan = helpers.make_plan(segments=[
            {"segment_id": "enr", "phase": "enroute",
             "geometry": {"type": "LineString", "coordinates": [[118.0, 39.0], [118.01, 39.0]]},
             "start": "2026-09-20T19:00:00+08:00", "end": "2026-09-20T20:00:00+08:00", "altitude_m": 120},
        ])
        helpers.submit(self.svc, "M-FAR", plan=far_plan)
        self.svc.put_notice(
            "N-NEW3",
            {
                "version": 1, "kind": "no_fly", "title": "突发禁飞",
                "geometry": helpers.square(116.0, 39.0, 116.01, 39.01),
                "effective_from": "2026-09-20T11:30:00+08:00",
            },
        )
        reviews = self.svc.list_reviews(status="pending")
        self.assertEqual(len(reviews), 1)  # 只有 M-RELEASED,没有 M-FAR


if __name__ == "__main__":
    unittest.main()
