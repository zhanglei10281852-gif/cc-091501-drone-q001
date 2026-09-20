import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from clearance import timeutil  # noqa: E402


class ParseInstantTest(unittest.TestCase):
    def test_z_suffix_and_offset_normalize_to_same_utc(self):
        a = timeutil.parse_instant("2026-09-20T20:00:00+08:00")
        b = timeutil.parse_instant("2026-09-20T12:00:00Z")
        self.assertEqual(a, b)

    def test_naive_input_uses_default_timezone(self):
        dt = timeutil.parse_instant("2026-09-20T20:00:00")
        self.assertEqual(dt, timeutil.parse_instant("2026-09-20T12:00:00Z"))

    def test_epoch_seconds(self):
        dt = timeutil.parse_instant(0)
        self.assertEqual(dt.isoformat(), "1970-01-01T00:00:00+00:00")

    def test_invalid_input_rejected(self):
        with self.assertRaises(ValueError):
            timeutil.parse_instant("not-a-time")


class HalfOpenWindowTest(unittest.TestCase):
    """半开区间 [start, end):边界时刻的归属必须确定。"""

    def test_touching_windows_do_not_overlap(self):
        a_start = timeutil.parse_instant("2026-09-20T10:00:00Z")
        a_end = timeutil.parse_instant("2026-09-20T11:00:00Z")
        b_start = timeutil.parse_instant("2026-09-20T11:00:00Z")
        b_end = timeutil.parse_instant("2026-09-20T12:00:00Z")
        self.assertFalse(timeutil.overlaps(a_start, a_end, b_start, b_end))
        self.assertFalse(timeutil.overlaps(b_start, b_end, a_start, a_end))

    def test_partial_overlap(self):
        a_start = timeutil.parse_instant("2026-09-20T10:00:00Z")
        a_end = timeutil.parse_instant("2026-09-20T11:00:00Z")
        b_start = timeutil.parse_instant("2026-09-20T10:59:00Z")
        b_end = timeutil.parse_instant("2026-09-20T12:00:00Z")
        self.assertTrue(timeutil.overlaps(a_start, a_end, b_start, b_end))


class DailyWindowTest(unittest.TestCase):
    """每日窗口跨午夜展开。"""

    def test_overnight_window_covers_after_midnight(self):
        windows = [{"start": "22:00", "end": "06:00"}]
        # 任务段:9-20 23:30 → 9-21 00:30 (本地),跨午夜
        start = timeutil.parse_instant("2026-09-20T23:30:00+08:00")
        end = timeutil.parse_instant("2026-09-21T00:30:00+08:00")
        hits = timeutil.expand_daily_windows(windows, start, end)
        self.assertEqual(len(hits), 1)
        w_start, w_end = hits[0]
        self.assertEqual(w_start, timeutil.parse_instant("2026-09-20T22:00:00+08:00"))
        self.assertEqual(w_end, timeutil.parse_instant("2026-09-21T06:00:00+08:00"))

    def test_window_end_is_exclusive(self):
        windows = [{"start": "22:00", "end": "06:00"}]
        # 段开始时刻恰好等于窗口结束时刻 → 不相交(半开)
        start = timeutil.parse_instant("2026-09-21T06:00:00+08:00")
        end = timeutil.parse_instant("2026-09-21T07:00:00+08:00")
        self.assertEqual(timeutil.expand_daily_windows(windows, start, end), [])

    def test_segment_ending_at_window_start_is_clear(self):
        windows = [{"start": "22:00", "end": "06:00"}]
        start = timeutil.parse_instant("2026-09-20T21:00:00+08:00")
        end = timeutil.parse_instant("2026-09-20T22:00:00+08:00")
        self.assertEqual(timeutil.expand_daily_windows(windows, start, end), [])

    def test_same_day_window(self):
        windows = [{"start": "08:00", "end": "18:00"}]
        start = timeutil.parse_instant("2026-09-20T17:00:00+08:00")
        end = timeutil.parse_instant("2026-09-20T19:00:00+08:00")
        self.assertEqual(len(timeutil.expand_daily_windows(windows, start, end)), 1)

    def test_multi_day_mission_collects_each_night(self):
        windows = [{"start": "22:00", "end": "06:00"}]
        start = timeutil.parse_instant("2026-09-20T12:00:00+08:00")
        end = timeutil.parse_instant("2026-09-23T12:00:00+08:00")
        self.assertEqual(len(timeutil.expand_daily_windows(windows, start, end)), 3)


if __name__ == "__main__":
    unittest.main()
