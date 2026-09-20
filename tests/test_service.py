import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from release.service import (  # noqa: E402
    ReleaseService,
    ServiceConfig,
    ValidationError,
)
from release.store import ConflictError, NotFoundError  # noqa: E402

TZ8 = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=TZ8)


def T(text, day=0):
    return (datetime.fromisoformat(f"2026-09-20T{text}+08:00") + day * DAY).isoformat()


DAY = timedelta(days=1)


def square_geo(lon0, lat0, lon1, lat1):
    return {
        "type": "Polygon",
        "coordinates": [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]],
    }


def survey_plan(lon0=121.0002, lat0=31.0005, lon1=121.0008, lat1=31.0005,
                start="17:30:00", end="18:30:00", end_day=0, alt=80):
    return {
        "category": "survey",
        "aircraft": {"model": "M350", "max_altitude_m": 500, "max_range_m": 100000,
                     "max_flight_time_s": 7200, "hover_capable": True},
        "operator": {"id": "op1", "categories": ["survey"], "valid_until": None},
        "segments": [
            {"id": "takeoff", "type": "takeoff", "points": [
                {"lon": lon0, "lat": lat0, "alt_m": 0, "time": T(start)},
                {"lon": lon0, "lat": lat0, "alt_m": alt, "time": T(start)},
            ]},
            {"id": "enroute", "type": "enroute", "points": [
                {"lon": lon0, "lat": lat0, "alt_m": alt, "time": T(start)},
                {"lon": lon1, "lat": lat1, "alt_m": alt, "time": T(end, end_day)},
            ]},
            {"id": "hover", "type": "hover", "duration_s": 60, "points": [
                {"lon": lon1, "lat": lat1, "alt_m": alt, "time": T(end, end_day)},
            ]},
            {"id": "alternate", "type": "alternate", "points": [
                {"lon": lon1, "lat": lat1, "alt_m": alt, "time": T(end, end_day)},
                {"lon": lon0, "lat": lat0, "alt_m": 0, "time": T(end, end_day)},
            ]},
        ],
    }


def no_fly_notice(notice_id="NOTAM-1", effective_from="18:00:00", effective_until="23:30:00",
                  lon0=121.000, lat0=31.000, lon1=121.001, lat1=31.001):
    return {
        "id": notice_id, "op": "publish", "kind": "no_fly",
        "geometry": square_geo(lon0, lat0, lon1, lat1),
        "effective_from": T(effective_from),
        "effective_until": T(effective_until) if effective_until else None,
        "reason": "大型活动临时禁飞",
    }


class FakeClock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.service = self.make_service()

    def make_service(self):
        svc = ReleaseService(
            ServiceConfig(data_dir=self.tmp.name, scheduler_interval_s=0.05),
            clock=self.clock,
        )
        self.addCleanup(svc.close)
        return svc


class SubmitAndSnapshotTest(ServiceTestBase):
    def test_submit_approved_and_snapshot_pinned(self):
        # 空域已登记但与计划不相交：快照应固化其版本，结论为批准
        self.service.add_airspace({
            "id": "Z-FAR", "kind": "no_fly",
            "geometry": square_geo(120.5, 31.5, 120.6, 31.6),
        })
        out = self.service.submit_mission({
            "mission_id": "M1", "plan": survey_plan(),
            "decided_by": "放行员张三", "human_note": "常规测绘",
        })
        decision = out["decision"]
        self.assertEqual(decision["verdict"], "approved")
        self.assertEqual(decision["system_verdict"], "approved")
        snapshot = decision["snapshot"]
        self.assertEqual(snapshot["ruleset_version"], "release-rules/1.0.0")
        self.assertEqual(snapshot["airspace"][0]["id"], "Z-FAR")
        self.assertEqual(snapshot["airspace"][0]["version"], 1)
        self.assertTrue(snapshot["airspace"][0]["checksum"])
        self.assertEqual(snapshot["aircraft"]["model"], "M350")
        self.assertEqual(decision["decided_by"], "放行员张三")
        self.assertEqual(decision["human_note"], "常规测绘")
        # 分段审查结果齐全：起飞/航路/悬停/备降
        labels = {s["label"] for s in decision["segments"]}
        self.assertEqual(labels, {"起飞", "航路", "悬停", "备降"})

    def test_human_override_requires_note(self):
        with self.assertRaises(ValidationError):
            self.service.submit_mission({
                "mission_id": "M1", "plan": survey_plan(),
                "verdict_override": "conditional",
            })

    def test_human_override_recorded(self):
        out = self.service.submit_mission({
            "mission_id": "M1", "plan": survey_plan(),
            "decided_by": "放行员张三", "human_note": "活动临近，附加目视观察要求",
            "verdict_override": "conditional",
        })
        decision = out["decision"]
        self.assertEqual(decision["system_verdict"], "approved")
        self.assertEqual(decision["verdict"], "conditional")

    def test_naive_time_rejected(self):
        plan = survey_plan()
        plan["segments"][0]["points"][0]["time"] = "2026-09-20 17:30:00"
        with self.assertRaises(ValidationError):
            self.service.submit_mission({"mission_id": "M1", "plan": plan})

    def test_plan_version_conflict(self):
        self.service.submit_mission({"mission_id": "M1", "plan": survey_plan()})
        out = self.service.submit_plan("M1", {"plan": survey_plan(), "expected_version": 2})
        self.assertEqual(out["plan_version"], 2)
        with self.assertRaises(ConflictError):
            self.service.submit_plan("M1", {"plan": survey_plan(), "expected_version": 2})
        with self.assertRaises(NotFoundError):
            self.service.submit_plan("NOPE", {"plan": survey_plan(), "expected_version": 1})

    def test_decision_traceable(self):
        out = self.service.submit_mission({"mission_id": "M1", "plan": survey_plan()})
        decision_id = out["decision"]["id"]
        fetched = self.service.get_decision(decision_id)
        self.assertEqual(fetched["id"], decision_id)
        self.assertIn("snapshot", fetched)
        self.assertEqual(len(self.service.list_decisions("M1")), 1)


class ConcurrencyTest(ServiceTestBase):
    def test_concurrent_same_mission_single_version(self):
        plan = survey_plan()
        barrier = threading.Barrier(8)
        results, errors = [], []

        def submit():
            barrier.wait()
            try:
                self.service.submit_mission({"mission_id": "M1", "plan": plan})
                results.append("ok")
            except ConflictError:
                errors.append("conflict")

        threads = [threading.Thread(target=submit) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 1, "相同任务的并发提交只能产生一个有效版本")
        self.assertEqual(len(errors), 7)
        self.assertEqual(len(self.service.list_decisions("M1")), 1)

    def test_concurrent_plan_versions_single_winner(self):
        self.service.submit_mission({"mission_id": "M1", "plan": survey_plan()})
        barrier = threading.Barrier(6)
        wins = []

        def submit():
            barrier.wait()
            try:
                self.service.submit_plan("M1", {"plan": survey_plan(), "expected_version": 2})
                wins.append(1)
            except ConflictError:
                pass

        threads = [threading.Thread(target=submit) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(wins), 1)
        self.assertEqual(self.service.get_mission("M1")["current_plan_version"], 2)


class NoticeLifecycleTest(ServiceTestBase):
    def _approve_three_missions(self):
        for i in range(1, 4):
            self.service.submit_mission({"mission_id": f"M{i}", "plan": survey_plan()})

    def test_story_three_missions_span_notice_start(self):
        """大型活动通告 18:00 生效，三支已放行测绘任务横跨生效时刻。"""
        self._approve_three_missions()
        # 通告在未来生效：只登记定时事件，暂不产生复核
        out = self.service.publish_notice(no_fly_notice())
        self.assertEqual(out["version"], 1)
        self.assertIn("scheduled_event", out)
        self.assertEqual(self.service.list_reviews("pending"), [])

        # 到达生效时刻（定时生效工作）：生成 3 条复核 + 3 条提醒
        self.clock.advance(hours=6, minutes=5)  # 18:05
        self.service.process_due_events()
        reviews = self.service.list_reviews("pending")
        self.assertEqual(len(reviews), 3)
        self.assertEqual({r["mission_id"] for r in reviews}, {"M1", "M2", "M3"})
        reminders = self.service.list_reminders("open")
        self.assertEqual(len(reminders), 3)
        self.assertIn("NOTAM-1", reminders[0]["message"])

        # 原决策不被暗中改写
        for i in range(1, 4):
            mission = self.service.get_mission(f"M{i}")
            self.assertEqual(mission["latest_decision"]["verdict"], "approved")
            self.assertEqual(mission["open_reviews"], 1)

        # 复核并重新评估：通告已生效，新决策拒绝；旧决策仍可追溯
        review = reviews[0]
        resolved = self.service.resolve_review(review["id"], {
            "resolved_by": "放行员李四", "note": "通告生效，重新评估", "re_evaluate": True,
        })
        self.assertEqual(resolved["status"], "resolved")
        new_decision = self.service.get_decision(resolved["resolution"]["new_decision_id"])
        self.assertEqual(new_decision["verdict"], "rejected")
        old_decision = self.service.get_decision(review["decision_id"])
        self.assertEqual(old_decision["verdict"], "approved")  # 历史结论保持原样
        self.assertEqual(len(self.service.list_reminders("open")), 2)  # 其余两条提醒仍在

    def test_immediate_notice_scans_at_once(self):
        self._approve_three_missions()
        out = self.service.publish_notice(no_fly_notice(effective_from="11:00:00"))
        self.assertEqual(out["affected"], 3)
        self.assertEqual(len(self.service.list_reviews("pending")), 3)

    def test_notice_outside_plan_no_review(self):
        self._approve_three_missions()
        out = self.service.publish_notice(
            no_fly_notice(effective_from="11:00:00", lon0=122.0, lon1=122.001)
        )
        self.assertEqual(out["affected"], 0)
        self.assertEqual(self.service.list_reviews("pending"), [])

    def test_withdrawal_stable_rule(self):
        # 通告生效中提交：拒绝
        self.service.publish_notice(no_fly_notice(effective_from="11:00:00"))
        out = self.service.submit_mission({"mission_id": "M1", "plan": survey_plan()})
        self.assertEqual(out["decision"]["verdict"], "rejected")
        rejected_id = out["decision"]["id"]

        # 撤回（新版本）：重新评估即通过；撤回本身不改写旧决策
        w = self.service.publish_notice({"id": "NOTAM-1", "op": "withdraw", "reason": "活动取消"})
        self.assertEqual(w["version"], 2)
        out2 = self.service.evaluate_mission("M1", {"decided_by": "放行员张三"})
        self.assertEqual(out2["verdict"], "approved")
        self.assertEqual(out2["snapshot"]["notices"], [])
        old = self.service.get_decision(rejected_id)
        self.assertEqual(old["verdict"], "rejected")
        self.assertEqual(old["snapshot"]["notices"], [{"id": "NOTAM-1", "version": 1}])

    def test_notice_amendment_uses_latest_version(self):
        self.service.publish_notice(no_fly_notice(effective_from="11:00:00"))
        # 第 2 版把范围挪走：评估应采用最新版
        amended = no_fly_notice(effective_from="11:00:00", lon0=122.0, lon1=122.001)
        out = self.service.publish_notice(amended)
        self.assertEqual(out["version"], 2)
        out = self.service.submit_mission({"mission_id": "M1", "plan": survey_plan()})
        self.assertEqual(out["decision"]["verdict"], "approved")
        self.assertEqual(out["decision"]["snapshot"]["notices"], [{"id": "NOTAM-1", "version": 2}])


class RestartRecoveryTest(ServiceTestBase):
    def test_restart_continues_scheduled_and_pending_work(self):
        self.service.submit_mission({"mission_id": "M1", "plan": survey_plan()})
        self.service.publish_notice(no_fly_notice())  # 18:00 生效，登记定时事件
        self.service.close()

        # 重启：新实例、同一数据目录；定时事件仍在
        self.clock.advance(hours=6, minutes=5)
        svc2 = self.make_service()
        svc2.process_due_events()
        reviews = svc2.list_reviews("pending")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["mission_id"], "M1")
        svc2.close()

        # 再次重启：未完成的复核仍在，可继续处理
        svc3 = self.make_service()
        pending = svc3.list_reviews("pending")
        self.assertEqual(len(pending), 1)
        resolved = svc3.resolve_review(pending[0]["id"], {
            "resolved_by": "值班员", "note": "重启后继续复核", "re_evaluate": True,
        })
        self.assertEqual(resolved["status"], "resolved")
        decisions = svc3.list_decisions("M1")
        self.assertEqual(len(decisions), 2)  # 原批准 + 复核后新决策，全程可追溯

    def test_scheduler_thread_fires_event(self):
        self.service.submit_mission({"mission_id": "M1", "plan": survey_plan()})
        effective = (self.clock.now + timedelta(seconds=0.3)).strftime("%H:%M:%S.%f")
        self.service.publish_notice(no_fly_notice(effective_from=effective))
        self.clock.advance(seconds=1)  # 假时钟推进到事件到期之后
        self.service.start_scheduler()
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.service.list_reviews("pending"):
                break
            time.sleep(0.05)
        self.assertEqual(len(self.service.list_reviews("pending")), 1)


class ValidationTest(ServiceTestBase):
    def test_invalid_zone(self):
        with self.assertRaises(ValidationError):
            self.service.add_airspace({"id": "Z", "kind": "no_fly"})  # 缺 geometry
        with self.assertRaises(ValidationError):
            self.service.add_airspace({
                "id": "Z", "kind": "restricted", "geometry": square_geo(0, 0, 1, 1),
            })  # 限飞区缺上限

    def test_invalid_notice(self):
        with self.assertRaises(ValidationError):
            self.service.publish_notice({"id": "N", "op": "publish", "kind": "no_fly"})
        with self.assertRaises(ValidationError):
            bad = no_fly_notice()
            bad["effective_until"] = bad["effective_from"]
            self.service.publish_notice(bad)

    def test_unknown_mission(self):
        with self.assertRaises(NotFoundError):
            self.service.get_mission("NOPE")
        with self.assertRaises(NotFoundError):
            self.service.evaluate_mission("NOPE", {})


if __name__ == "__main__":
    unittest.main()
