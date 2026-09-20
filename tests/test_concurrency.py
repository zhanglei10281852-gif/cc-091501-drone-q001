"""并发:相同任务的并发提交只产生一个有效版本;幂等键重放安全。"""
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402
from clearance.service import ApiError  # noqa: E402


class ConcurrentSubmissionTest(unittest.TestCase):
    def setUp(self):
        self.svc = helpers.make_service()
        helpers.register_basics(self.svc)

    def _submit_with_key(self, mission_id, key, results, index):
        try:
            results[index] = ("ok", helpers.submit(self.svc, mission_id, key=key))
        except ApiError as exc:
            results[index] = ("error", exc)

    def test_concurrent_new_submissions_yield_single_version(self):
        threads = []
        results = [None] * 12
        for i in range(12):
            threads.append(
                threading.Thread(
                    target=self._submit_with_key, args=("M-RACE", f"race-key-{i}", results, i)
                )
            )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r[0] == "ok"]
        conflicts = [r for r in results if r[0] == "error" and r[1].status == 409]
        # 恰好一个提交成功,其余全部 409
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 11)
        self.assertEqual(successes[0][1]["plan_version"], 1)
        # 任务只有一个有效版本
        mission = self.svc.get_mission("M-RACE")
        self.assertEqual(len(mission["plans"]), 1)

    def test_same_idempotency_key_replays_same_decision(self):
        threads = []
        results = [None] * 8
        for i in range(8):
            threads.append(
                threading.Thread(
                    target=self._submit_with_key, args=("M-IDEM", "shared-key", results, i)
                )
            )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertTrue(all(r[0] == "ok" for r in results))
        decision_ids = {r[1]["decision_id"] for r in results}
        self.assertEqual(len(decision_ids), 1)
        self.assertTrue(any(r[1]["replayed"] for r in results))
        mission = self.svc.get_mission("M-IDEM")
        self.assertEqual(len(mission["plans"]), 1)

    def test_idempotency_key_cannot_be_reused_by_other_mission(self):
        helpers.submit(self.svc, "M-A", key="dup-key")
        with self.assertRaises(ApiError) as ctx:
            helpers.submit(self.svc, "M-B", key="dup-key")
        self.assertEqual(ctx.exception.status, 409)

    def test_version_chain_requires_expected_base(self):
        first = helpers.submit(self.svc, "M-CHAIN")
        self.assertEqual(first["plan_version"], 1)
        # 基于过期版本号提交 → 409
        with self.assertRaises(ApiError) as ctx:
            helpers.submit(self.svc, "M-CHAIN", key="k2", base=0)
        self.assertEqual(ctx.exception.status, 409)
        # 基于当前版本提交 → v2,且与 v1 形成修订链
        second = helpers.submit(self.svc, "M-CHAIN", key="k2-ok", base=1)
        self.assertEqual(second["plan_version"], 2)
        decision = self.svc.get_decision(second["decision_id"])
        self.assertEqual(decision["revision_of"], first["decision_id"])


if __name__ == "__main__":
    unittest.main()
