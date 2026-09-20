import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from release.store import ConflictError, NotFoundError, Store  # noqa: E402

NOW = "2026-09-20T04:00:00Z"


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "test.db")
        self.addCleanup(self.store.close)

    def test_zone_versions(self):
        v1 = self.store.add_zone("Z1", {"kind": "no_fly"}, "sum1", NOW)
        v2 = self.store.add_zone("Z1", {"kind": "restricted"}, "sum2", NOW)
        self.assertEqual((v1, v2), (1, 2))
        current = self.store.current_zones()
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["version"], 2)
        self.assertEqual(len(self.store.zone_history("Z1")), 2)

    def test_notice_chain(self):
        self.assertEqual(self.store.add_notice("N1", {"op": "publish"}, NOW), 1)
        self.assertEqual(self.store.add_notice("N1", {"op": "withdraw"}, NOW), 2)
        current = self.store.current_notices()
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["version"], 2)
        self.assertEqual(self.store.get_notice_version("N1", 1)["version"], 1)

    def test_mission_create_conflict(self):
        self.store.create_mission("M1", "survey", {"segments": []}, NOW)
        with self.assertRaises(ConflictError):
            self.store.create_mission("M1", "survey", {"segments": []}, NOW)

    def test_plan_version_conflict(self):
        self.store.create_mission("M1", "survey", {"v": 1}, NOW)
        # 当前最新为第 1 版，期望第 2 版：成功
        self.assertEqual(self.store.add_plan("M1", 2, {"v": 2}, NOW), 2)
        # 版本错位：拒绝
        with self.assertRaises(ConflictError):
            self.store.add_plan("M1", 2, {"v": 2}, NOW)
        with self.assertRaises(ConflictError):
            self.store.add_plan("M1", 5, {"v": 5}, NOW)
        with self.assertRaises(NotFoundError):
            self.store.add_plan("NOPE", 1, {"v": 1}, NOW)

    def test_decisions_append_only(self):
        self.store.create_mission("M1", "survey", {"v": 1}, NOW)
        record = {"system_verdict": "approved", "verdict": "approved"}
        s1 = self.store.append_decision("dec-1", "M1", 1, record, NOW)
        s2 = self.store.append_decision("dec-2", "M1", 1, record, NOW)
        self.assertEqual((s1, s2), (1, 2))
        self.assertEqual(self.store.latest_decision("M1")["id"], "dec-2")
        self.assertEqual(len(self.store.decisions_for("M1")), 2)

    def test_review_dedup_and_reminder(self):
        self.store.create_mission("M1", "survey", {"v": 1}, NOW)
        self.store.append_decision(
            "dec-1", "M1", 1, {"system_verdict": "approved", "verdict": "approved"}, NOW
        )
        created = self.store.create_review_with_reminder(
            "rev-1", "M1", "dec-1", "notice", "N1@1", "rem-1", "请复核", NOW
        )
        self.assertTrue(created)
        again = self.store.create_review_with_reminder(
            "rev-2", "M1", "dec-1", "notice", "N1@1", "rem-2", "请复核", NOW
        )
        self.assertFalse(again)  # 同一触发源不重复生成
        self.assertEqual(len(self.store.list_reminders()), 1)
        self.assertEqual(len(self.store.list_reviews("pending")), 1)
        self.store.resolve_review("rev-1", {"resolved_by": "张三"}, NOW)
        self.assertEqual(self.store.list_reviews("pending"), [])
        with self.assertRaises(ConflictError):
            self.store.resolve_review("rev-1", {"resolved_by": "李四"}, NOW)

    def test_scheduled_events(self):
        self.store.schedule_event("evt-1", "notice_effective", "2026-09-20T10:00:00Z", {"notice_id": "N1", "version": 1})
        self.assertEqual(self.store.due_events("2026-09-20T09:00:00Z"), [])
        due = self.store.due_events("2026-09-20T10:00:00Z")
        self.assertEqual([e["id"] for e in due], ["evt-1"])
        self.store.mark_event_done("evt-1", "2026-09-20T10:00:01Z")
        self.assertEqual(self.store.pending_events(), [])

    def test_persistence_across_reopen(self):
        self.store.create_mission("M1", "survey", {"v": 1}, NOW)
        self.store.close()
        reopened = Store(Path(self.tmp.name) / "test.db")
        self.addCleanup(reopened.close)
        self.assertIsNotNone(reopened.get_mission("M1"))


if __name__ == "__main__":
    unittest.main()
