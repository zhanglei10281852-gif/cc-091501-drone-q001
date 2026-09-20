import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from release.geometry import (  # noqa: E402
    haversine_m,
    normalize_ring,
    point_in_polygon,
    segment_polygon_intervals,
)

SQUARE = [normalize_ring([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]])]
# 带洞正方形：外环 (0,0)-(10,10)，洞 (3,3)-(7,7)
DONUT = [
    normalize_ring([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]),
    normalize_ring([[3, 3], [7, 3], [7, 7], [3, 7], [3, 3]]),
]


class PointInPolygonTest(unittest.TestCase):
    def test_inside_outside(self):
        self.assertTrue(point_in_polygon((5, 5), SQUARE))
        self.assertFalse(point_in_polygon((15, 5), SQUARE))

    def test_boundary_is_inside_closed_set(self):
        self.assertTrue(point_in_polygon((0, 5), SQUARE))   # 边上
        self.assertTrue(point_in_polygon((10, 10), SQUARE))  # 顶点

    def test_hole(self):
        self.assertFalse(point_in_polygon((5, 5), DONUT))   # 洞内部不在空域内
        self.assertTrue(point_in_polygon((3, 5), DONUT))    # 洞边界仍属空域（闭集）
        self.assertTrue(point_in_polygon((1, 1), DONUT))    # 环带内


class SegmentPolygonIntervalsTest(unittest.TestCase):
    def test_crossing(self):
        self.assertEqual(segment_polygon_intervals((-5, 5), (15, 5), SQUARE), [(0.25, 0.75)])

    def test_fully_inside_and_outside(self):
        self.assertEqual(segment_polygon_intervals((2, 2), (8, 8), SQUARE), [(0.0, 1.0)])
        self.assertEqual(segment_polygon_intervals((-5, -5), (-1, -1), SQUARE), [])

    def test_tangent_touch_counts(self):
        # 直线 y = -x + 20 仅在顶点 (10,10) 处与正方形相切
        intervals = segment_polygon_intervals((5, 15), (15, 5), SQUARE)
        self.assertEqual(intervals, [(0.5, 0.5)])

    def test_endpoint_touch_counts(self):
        intervals = segment_polygon_intervals((5, -5), (10, 0), SQUARE)
        self.assertEqual(intervals, [(1.0, 1.0)])

    def test_collinear_edge_overlap(self):
        intervals = segment_polygon_intervals((-5, 0), (15, 0), SQUARE)
        self.assertEqual(intervals, [(0.25, 0.75)])

    def test_degenerate_point_segment(self):
        self.assertEqual(segment_polygon_intervals((5, 5), (5, 5), SQUARE), [(0.0, 1.0)])
        self.assertEqual(segment_polygon_intervals((50, 50), (50, 50), SQUARE), [])

    def test_hole_pass_through(self):
        # 横穿环带：进外环 -> 进洞（离开空域）-> 出洞 -> 出外环
        intervals = segment_polygon_intervals((-5, 5), (15, 5), DONUT)
        self.assertEqual(intervals, [(0.25, 0.4), (0.6, 0.75)])

    def test_haversine_known_distance(self):
        # 赤道上 1 个经度约 111.195 km
        self.assertAlmostEqual(haversine_m((0, 0), (1, 0)), 111195, delta=500)


if __name__ == "__main__":
    unittest.main()
