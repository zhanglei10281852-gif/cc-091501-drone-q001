import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from release import rules  # noqa: E402
from release.geometry import normalize_ring  # noqa: E402

TZ8 = timezone(timedelta(hours=8))
DAY2 = timedelta(days=1)


def T(text, day=0):
    return datetime.fromisoformat(f"2026-09-20T{text}+08:00") + day * DAY2


def square(lon0, lat0, lon1, lat1):
    """单个多边形（含一个外环）。"""
    return [normalize_ring([[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]])]


def pt(lon, lat, alt, time_text, day=0):
    return {"lon": lon, "lat": lat, "alt_m": alt, "time": T(time_text, day)}


def seg(seg_id, seg_type, points, **kw):
    return {"id": seg_id, "type": seg_type, "points": points, "duration_s": kw.get("duration_s")}


def make_plan(segments, aircraft=None, operator=None, category="survey"):
    return {
        "category": category,
        "aircraft": aircraft or {
            "model": "M350", "max_altitude_m": 500, "max_range_m": 100000,
            "max_flight_time_s": 7200, "hover_capable": True,
        },
        "operator": operator or {"id": "op1", "categories": ["survey"], "valid_until": None},
        "segments": segments,
    }


def no_fly_zone(lon0=121.000, lat0=31.000, lon1=121.001, lat1=31.001, **kw):
    zone = {
        "id": "Z1", "version": 1, "checksum": "c1", "kind": "no_fly",
        "rings_list": [square(lon0, lat0, lon1, lat1)],
        "lower_m": None, "upper_m": None, "max_altitude_m": None,
        "active_from": None, "active_until": None, "daily_windows": None,
    }
    zone.update(kw)
    return zone


def notice(kind="no_fly", effective_from="18:00:00", effective_until="19:00:00",
           lon0=121.000, lat0=31.000, lon1=121.001, lat1=31.001, **kw):
    n = {
        "id": "N1", "version": 1, "kind": kind,
        "rings_list": [square(lon0, lat0, lon1, lat1)],
        "effective_from": T(effective_from),
        "effective_until": T(effective_until) if effective_until else None,
        "max_altitude_m": None,
    }
    n.update(kw)
    return n


def codes(result, seg_id):
    seg = next(s for s in result["segments"] if s["id"] == seg_id)
    return [f["code"] for f in seg["findings"]]


class AirspaceTest(unittest.TestCase):
    def test_clear_plan_approved(self):
        plan = make_plan([seg("s1", "enroute", [pt(120.0, 31.0, 80, "18:00:00"), pt(120.005, 31.0, 80, "18:05:00")])])
        result = rules.evaluate(plan, [no_fly_zone()], [], "Asia/Shanghai")
        self.assertEqual(result["verdict"], "approved")
        self.assertEqual(result["conditions"], [])
        self.assertEqual(result["reasons"], [])

    def test_enroute_crossing_no_fly_rejected(self):
        plan = make_plan([
            seg("s1", "takeoff", [pt(120.0, 31.0005, 0, "17:55:00"), pt(120.0, 31.0005, 80, "17:56:00")]),
            seg("s2", "enroute", [pt(120.0, 31.0005, 80, "17:56:00"), pt(121.002, 31.0005, 80, "18:05:00")]),
        ])
        result = rules.evaluate(plan, [no_fly_zone()], [], "Asia/Shanghai")
        self.assertEqual(result["verdict"], "rejected")
        self.assertEqual(result["segments"][0]["verdict"], "approved")  # 起飞段独立审查
        self.assertIn("ZONE_NO_FLY", codes(result, "s2"))

    def test_takeoff_inside_zone_rejected(self):
        plan = make_plan([seg("s1", "takeoff", [pt(121.0005, 31.0005, 0, "18:00:00"), pt(121.0005, 31.0005, 60, "18:01:00")])])
        result = rules.evaluate(plan, [no_fly_zone()], [], "Asia/Shanghai")
        self.assertEqual(result["verdict"], "rejected")
        self.assertIn("ZONE_NO_FLY", codes(result, "s1"))

    def test_alternate_route_also_reviewed(self):
        # 主航路干净，备降段穿区 —— 人工比对最容易漏的就是这条
        plan = make_plan([
            seg("s1", "enroute", [pt(120.0, 31.0, 80, "18:00:00"), pt(120.005, 31.0, 80, "18:10:00")]),
            seg("s9", "alternate", [pt(120.005, 31.0, 80, "18:10:00"), pt(121.002, 31.0005, 0, "18:20:00")]),
        ])
        result = rules.evaluate(plan, [no_fly_zone()], [], "Asia/Shanghai")
        self.assertEqual(result["verdict"], "rejected")
        self.assertIn("ZONE_NO_FLY", codes(result, "s9"))

    def test_boundary_tangency_is_conflict(self):
        # 航线与禁飞区顶点 (121.001, 31.001) 相切：闭集规则下算冲突
        plan = make_plan([seg("s1", "enroute", [
            pt(121.0005, 31.0015, 80, "18:00:00"), pt(121.0015, 31.0005, 80, "18:05:00"),
        ])])
        result = rules.evaluate(plan, [no_fly_zone()], [], "Asia/Shanghai")
        self.assertEqual(result["verdict"], "rejected")
        self.assertIn("ZONE_NO_FLY", codes(result, "s1"))

    def test_vertical_extent_closed(self):
        zone = no_fly_zone(lower_m=0, upper_m=100)
        above = make_plan([seg("s1", "enroute", [pt(121.0002, 31.0005, 150, "18:00:00"), pt(121.0008, 31.0005, 150, "18:02:00")])])
        self.assertEqual(rules.evaluate(above, [zone], [], "Asia/Shanghai")["verdict"], "approved")
        # 恰好压在区顶 100 米：闭区间，算冲突
        at_top = make_plan([seg("s1", "enroute", [pt(121.0002, 31.0005, 100, "18:00:00"), pt(121.0008, 31.0005, 100, "18:02:00")])])
        self.assertEqual(rules.evaluate(at_top, [zone], [], "Asia/Shanghai")["verdict"], "rejected")

    def test_restricted_zone_conditional_and_exceeded(self):
        zone = no_fly_zone(kind="restricted", max_altitude_m=100)
        below = make_plan([seg("s1", "enroute", [pt(121.0002, 31.0005, 80, "18:00:00"), pt(121.0008, 31.0005, 80, "18:02:00")])])
        result = rules.evaluate(below, [zone], [], "Asia/Shanghai")
        self.assertEqual(result["verdict"], "conditional")
        self.assertEqual(result["conditions"][0]["code"], "ZONE_RESTRICTED")
        over = make_plan([seg("s1", "enroute", [pt(121.0002, 31.0005, 150, "18:00:00"), pt(121.0008, 31.0005, 150, "18:02:00")])])
        result = rules.evaluate(over, [zone], [], "Asia/Shanghai")
        self.assertEqual(result["verdict"], "rejected")
        self.assertIn("ZONE_ALT_EXCEEDED", codes(result, "s1"))

    def test_daily_window_zone(self):
        zone = no_fly_zone(daily_windows=[(22 * 60, 6 * 60)])
        night = make_plan([seg("s1", "enroute", [pt(121.0002, 31.0005, 80, "23:00:00"), pt(121.0008, 31.0005, 80, "23:05:00")])])
        self.assertEqual(rules.evaluate(night, [zone], [], "Asia/Shanghai")["verdict"], "rejected")
        noon = make_plan([seg("s1", "enroute", [pt(121.0002, 31.0005, 80, "12:00:00"), pt(121.0008, 31.0005, 80, "12:05:00")])])
        self.assertEqual(rules.evaluate(noon, [zone], [], "Asia/Shanghai")["verdict"], "approved")


class NoticeTest(unittest.TestCase):
    def test_notice_window(self):
        n = notice()
        inside = make_plan([seg("s1", "enroute", [pt(121.0002, 31.0005, 80, "18:30:00"), pt(121.0008, 31.0005, 80, "18:35:00")])])
        self.assertEqual(rules.evaluate(inside, [], [n], "Asia/Shanghai")["verdict"], "rejected")
        after = make_plan([seg("s1", "enroute", [pt(121.0002, 31.0005, 80, "19:00:00"), pt(121.0008, 31.0005, 80, "19:05:00")])])
        self.assertEqual(rules.evaluate(after, [], [n], "Asia/Shanghai")["verdict"], "approved")

    def test_flight_spanning_notice_start(self):
        # 横跨通告生效时刻：17:50 起飞，18:10 仍在区内 —— 必须判冲突
        n = notice()
        plan = make_plan([seg("s1", "enroute", [pt(121.0002, 31.0005, 80, "17:50:00"), pt(121.0008, 31.0005, 80, "18:10:00")])])
        result = rules.evaluate(plan, [], [n], "Asia/Shanghai")
        self.assertEqual(result["verdict"], "rejected")
        self.assertIn("NOTICE_NO_FLY", codes(result, "s1"))

    def test_crossing_midnight(self):
        n = notice(effective_from="00:00:00", effective_until="01:00:00")
        n["effective_from"] = T("00:00:00", day=1)
        n["effective_until"] = T("01:00:00", day=1)
        plan = make_plan([seg("s1", "enroute", [
            pt(121.0002, 31.0005, 80, "23:50:00"), pt(121.0008, 31.0005, 80, "00:20:00", day=1),
        ])])
        result = rules.evaluate(plan, [], [n], "Asia/Shanghai")
        self.assertEqual(result["verdict"], "rejected")

    def test_altitude_limit_notice(self):
        n = notice(kind="altitude_limit", max_altitude_m=120)
        below = make_plan([seg("s1", "enroute", [pt(121.0002, 31.0005, 100, "18:30:00"), pt(121.0008, 31.0005, 100, "18:35:00")])])
        result = rules.evaluate(below, [], [n], "Asia/Shanghai")
        self.assertEqual(result["verdict"], "conditional")
        self.assertEqual(result["conditions"][0]["code"], "NOTICE_ALT_LIMIT")
        over = make_plan([seg("s1", "enroute", [pt(121.0002, 31.0005, 150, "18:30:00"), pt(121.0008, 31.0005, 150, "18:35:00")])])
        self.assertEqual(rules.evaluate(over, [], [n], "Asia/Shanghai")["verdict"], "rejected")

    def test_notice_conflicts_for_rescan(self):
        n = notice()
        hit = make_plan([seg("s1", "enroute", [pt(121.0002, 31.0005, 80, "18:30:00"), pt(121.0008, 31.0005, 80, "18:35:00")])])
        self.assertTrue(rules.notice_conflicts(hit, n, "Asia/Shanghai"))
        miss = make_plan([seg("s1", "enroute", [pt(120.0, 31.0, 80, "18:30:00"), pt(120.005, 31.0, 80, "18:35:00")])])
        self.assertFalse(rules.notice_conflicts(miss, n, "Asia/Shanghai"))
        # 限高通告下计划本身合规：不算"影响"，不应触发复核
        n_alt = notice(kind="altitude_limit", max_altitude_m=120)
        low = make_plan([seg("s1", "enroute", [pt(121.0002, 31.0005, 100, "18:30:00"), pt(121.0008, 31.0005, 100, "18:35:00")])])
        self.assertFalse(rules.notice_conflicts(low, n_alt, "Asia/Shanghai"))


class CapabilityAndOperatorTest(unittest.TestCase):
    def test_aircraft_altitude(self):
        aircraft = {"model": "mini", "max_altitude_m": 120, "max_range_m": None, "max_flight_time_s": None, "hover_capable": True}
        plan = make_plan([seg("s1", "takeoff", [pt(120.0, 31.0, 0, "18:00:00"), pt(120.0, 31.0, 200, "18:02:00")])], aircraft=aircraft)
        result = rules.evaluate(plan, [], [], "Asia/Shanghai")
        self.assertEqual(result["verdict"], "rejected")
        self.assertIn("AIRCRAFT_ALTITUDE", codes(result, "s1"))

    def test_hover_unsupported(self):
        aircraft = {"model": "fixed-wing", "max_altitude_m": None, "max_range_m": None, "max_flight_time_s": None, "hover_capable": False}
        plan = make_plan([seg("s1", "hover", [pt(120.0, 31.0, 80, "18:00:00")], duration_s=300)], aircraft=aircraft)
        result = rules.evaluate(plan, [], [], "Asia/Shanghai")
        self.assertEqual(result["verdict"], "rejected")
        self.assertIn("AIRCRAFT_HOVER_UNSUPPORTED", codes(result, "s1"))

    def test_range_exceeded(self):
        aircraft = {"model": "mini", "max_altitude_m": None, "max_range_m": 1000, "max_flight_time_s": None, "hover_capable": True}
        plan = make_plan([seg("s1", "enroute", [pt(120.0, 31.0, 80, "18:00:00"), pt(120.05, 31.0, 80, "18:30:00")])], aircraft=aircraft)
        result = rules.evaluate(plan, [], [], "Asia/Shanghai")
        self.assertEqual(result["verdict"], "rejected")
        self.assertIn("AIRCRAFT_RANGE", [f["code"] for f in result["mission_findings"]])

    def test_operator_license_expires_mid_flight(self):
        operator = {"id": "op1", "categories": ["survey"], "valid_until": T("18:05:00")}
        plan = make_plan([
            seg("s1", "enroute", [pt(120.0, 31.0, 80, "18:00:00"), pt(120.001, 31.0, 80, "18:04:00")]),
            seg("s2", "enroute", [pt(120.001, 31.0, 80, "18:04:00"), pt(120.002, 31.0, 80, "18:10:00")]),
        ], operator=operator)
        result = rules.evaluate(plan, [], [], "Asia/Shanghai")
        self.assertEqual(result["verdict"], "rejected")
        self.assertEqual(result["segments"][0]["verdict"], "approved")
        self.assertIn("OPERATOR_LICENSE_EXPIRED", codes(result, "s2"))

    def test_operator_license_expires_during_hover(self):
        # 单点悬停 10 分钟，资质在第 5 分钟到期：悬停段必须判拒绝
        operator = {"id": "op1", "categories": ["survey"], "valid_until": T("18:05:00")}
        plan = make_plan([
            seg("s1", "hover", [pt(120.0, 31.0, 80, "18:00:00")], duration_s=600),
        ], operator=operator)
        result = rules.evaluate(plan, [], [], "Asia/Shanghai")
        self.assertEqual(result["verdict"], "rejected")
        self.assertIn("OPERATOR_LICENSE_EXPIRED", codes(result, "s1"))

    def test_operator_category(self):
        plan = make_plan([seg("s1", "enroute", [pt(120.0, 31.0, 80, "18:00:00"), pt(120.001, 31.0, 80, "18:02:00")])], category="delivery")
        result = rules.evaluate(plan, [], [], "Asia/Shanghai")
        self.assertEqual(result["verdict"], "rejected")
        self.assertIn("OPERATOR_CATEGORY", [f["code"] for f in result["mission_findings"]])


if __name__ == "__main__":
    unittest.main()
