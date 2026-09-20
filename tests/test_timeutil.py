import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from release.timeutil import (  # noqa: E402
    daily_windows_overlap,
    intervals_overlap,
    iso_utc,
    parse_daily_window,
    parse_iso,
)

TZ8 = timezone(timedelta(hours=8))


def T(text):
    return datetime.fromisoformat(f"2026-09-20T{text}+08:00")


class ParseIsoTest(unittest.TestCase):
    def test_requires_timezone(self):
        with self.assertRaises(ValueError):
            parse_iso("2026-09-20 18:00:00")

    def test_accepts_z_and_offset(self):
        a = parse_iso("2026-09-20T18:00:00Z")
        b = parse_iso("2026-09-21T04:00:00+10:00")
        self.assertEqual(a, b)

    def test_iso_utc_roundtrip_sortable(self):
        self.assertEqual(iso_utc(T("18:00:00")), "2026-09-20T10:00:00Z")


class AbsoluteWindowTest(unittest.TestCase):
    def test_half_open_window(self):
        win_from, win_until = T("18:00:00"), T("19:00:00")
        # 飞行恰好在窗口结束时刻开始：不冲突
        self.assertFalse(intervals_overlap(T("19:00:00"), T("19:30:00"), win_from, win_until))
        # 飞行恰好在窗口开始时刻仍在场：冲突
        self.assertTrue(intervals_overlap(T("17:30:00"), T("18:00:00"), win_from, win_until))
        # 完全在窗口之前：不冲突
        self.assertFalse(intervals_overlap(T("16:00:00"), T("17:00:00"), win_from, win_until))

    def test_unbounded_window(self):
        self.assertTrue(intervals_overlap(T("00:00:00"), T("23:59:00"), None, None))
        self.assertFalse(intervals_overlap(T("00:00:00"), T("01:00:00"), T("02:00:00"), None))


class DailyWindowTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_daily_window("22:30"), 22 * 60 + 30)
        with self.assertRaises(ValueError):
            parse_daily_window("24:00")

    def test_wraps_midnight(self):
        windows = [(22 * 60, 6 * 60)]  # 22:00 - 次日 06:00
        self.assertTrue(daily_windows_overlap(T("23:00:00"), T("23:30:00"), windows, "Asia/Shanghai"))
        self.assertTrue(daily_windows_overlap(T("03:00:00") + timedelta(days=1), T("04:00:00") + timedelta(days=1), windows, "Asia/Shanghai"))
        self.assertFalse(daily_windows_overlap(T("12:00:00"), T("13:00:00"), windows, "Asia/Shanghai"))

    def test_flight_crossing_midnight(self):
        windows = [(22 * 60, 6 * 60)]
        start, end = T("23:30:00"), T("00:30:00") + timedelta(days=1)
        self.assertTrue(daily_windows_overlap(start, end, windows, "Asia/Shanghai"))

    def test_daytime_window(self):
        windows = [(9 * 60, 17 * 60)]
        self.assertFalse(daily_windows_overlap(T("18:00:00"), T("19:00:00"), windows, "Asia/Shanghai"))
        self.assertTrue(daily_windows_overlap(T("16:30:00"), T("17:30:00"), windows, "Asia/Shanghai"))


if __name__ == "__main__":
    unittest.main()
