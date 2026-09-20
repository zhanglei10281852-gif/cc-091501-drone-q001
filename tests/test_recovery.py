"""重启恢复:定时生效工作与未完成复核在重启后继续。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402
from clearance.timeutil import parse_instant  # noqa: E402


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "clearance.db")

    def test_restart_resumes_scheduled_notice_activation(self):
        # 第一次运行:提交已放行任务 + 未来生效的通告
        svc1 = helpers.make_service(self.db_path)
        helpers.register_basics(svc1)
        plan = helpers.make_plan(segments=[
            {"segment_id": "enr", "phase": "enroute",
             "geometry": {"type": "LineString", "coordinates": [[116.0, 39.005], [116.01, 39.005]]},
             "start": "2026-09-21T19:00:00+08:00", "end": "2026-09-21T20:00:00+08:00", "altitude_m": 120},
        ])
        approved = helpers.submit(svc1, "M-FUTURE", plan=plan)
        self.assertEqual(approved["verdict"], "APPROVED")
        svc1.put_notice(
            "N-FUTURE",
            {
                "version": 1, "kind": "no_fly", "title": "次日活动禁飞",
                "geometry": helpers.square(116.0, 39.0, 116.01, 39.01),
                "effective_from": "2026-09-21T18:00:00+08:00",
                "effective_to": "2026-09-21T23:00:00+08:00",
            },
        )
        # 通告未生效,暂无复核任务
        self.assertEqual(svc1.list_reviews(status="pending"), [])
        svc1.store.close()

        # 模拟重启:同一数据库、时钟已走过通告生效时刻
        later = parse_instant("2026-09-21T18:05:00+08:00").timestamp()
        svc2 = helpers.make_service(self.db_path, now=later)
        svc2.recover()  # 启动恢复:补跑到期的定时工作
        reviews = svc2.list_reviews(status="pending")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["notice_id"], "N-FUTURE")
        self.assertEqual(reviews[0]["decision_id"], approved["decision_id"])
        # 原决定仍未被改写
        self.assertEqual(svc2.get_decision(approved["decision_id"])["verdict"], "APPROVED")
        # 提醒也已生成
        self.assertEqual(len(svc2.list_reminders()), 1)
        svc2.store.close()

    def test_restart_keeps_unfinished_reviews(self):
        svc1 = helpers.make_service(self.db_path)
        helpers.register_basics(svc1)
        plan = helpers.make_plan(segments=[
            {"segment_id": "enr", "phase": "enroute",
             "geometry": {"type": "LineString", "coordinates": [[116.0, 39.005], [116.01, 39.005]]},
             "start": "2026-09-20T19:00:00+08:00", "end": "2026-09-20T20:00:00+08:00", "altitude_m": 120},
        ])
        helpers.submit(svc1, "M-REVIEW", plan=plan)
        svc1.put_notice(
            "N-IMM",
            {
                "version": 1, "kind": "no_fly", "title": "立即生效禁飞",
                "geometry": helpers.square(116.0, 39.0, 116.01, 39.01),
                "effective_from": "2026-09-20T11:00:00+08:00",
            },
        )
        self.assertEqual(len(svc1.list_reviews(status="pending")), 1)
        svc1.store.close()

        svc2 = helpers.make_service(self.db_path)
        pending = svc2.list_reviews(status="pending")
        self.assertEqual(len(pending), 1)
        resolved = svc2.resolve_review(pending[0]["review_id"], actor="dispatcher", note="重启后办结")
        self.assertEqual(resolved["status"], "resolved")
        svc2.store.close()

    def test_notice_withdrawn_before_activation_skips_scan(self):
        svc1 = helpers.make_service(self.db_path)
        helpers.register_basics(svc1)
        helpers.submit(svc1, "M-SKIP")
        svc1.put_notice(
            "N-CANCEL",
            {
                "version": 1, "kind": "no_fly", "title": "未来禁飞",
                "geometry": helpers.square(117.0, 39.0, 117.01, 39.01),
                "effective_from": "2026-09-21T18:00:00+08:00",
            },
        )
        svc1.withdraw_notice("N-CANCEL", actor="dispatcher", reason="活动取消")
        svc1.store.close()

        later = parse_instant("2026-09-21T19:00:00+08:00").timestamp()
        svc2 = helpers.make_service(self.db_path, now=later)
        svc2.recover()
        # 生效前已撤回:到期只留审计,不产生复核任务
        self.assertEqual(svc2.list_reviews(status="pending"), [])
        svc2.store.close()


if __name__ == "__main__":
    unittest.main()
