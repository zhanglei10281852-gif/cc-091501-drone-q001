import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from clearance import geometry  # noqa: E402

SQUARE = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]],
}


def rings_of(geom):
    return geometry.normalize_polygons(geom)[0]


class PointInPolygonTest(unittest.TestCase):
    def setUp(self):
        self.rings = rings_of(SQUARE)

    def test_interior_point(self):
        self.assertTrue(geometry.point_in_polygon((0.5, 0.5), self.rings))

    def test_exterior_point(self):
        self.assertFalse(geometry.point_in_polygon((2.0, 0.5), self.rings))

    def test_boundary_point_belongs_to_closed_region(self):
        self.assertTrue(geometry.point_in_polygon((0.5, 0.0), self.rings))
        self.assertTrue(geometry.point_in_polygon((1.0, 0.5), self.rings))

    def test_vertex_belongs_to_closed_region(self):
        self.assertTrue(geometry.point_in_polygon((0.0, 0.0), self.rings))
        self.assertTrue(geometry.point_in_polygon((1.0, 1.0), self.rings))

    def test_hole_interior_is_outside_region(self):
        geom = {
            "type": "Polygon",
            "coordinates": [
                [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0], [0.0, 0.0]],
                [[1.0, 1.0], [3.0, 1.0], [3.0, 3.0], [1.0, 3.0], [1.0, 1.0]],
            ],
        }
        rings = rings_of(geom)
        self.assertFalse(geometry.point_in_polygon((2.0, 2.0), rings))  # 洞内部
        self.assertTrue(geometry.point_in_polygon((0.5, 0.5), rings))  # 环与洞之间
        # 稳定规则:洞的边界属于区域(对禁飞区取保守方向)
        self.assertTrue(geometry.point_in_polygon((1.0, 2.0), rings))


class SegmentIntersectionTest(unittest.TestCase):
    def setUp(self):
        self.rings = rings_of(SQUARE)

    def test_crossing_segment(self):
        self.assertTrue(geometry.segment_intersects_polygon((-1.0, 0.5), (2.0, 0.5), self.rings))

    def test_tangent_segment_counts_as_conflict(self):
        """边界相切:航线与区域边界相触即判冲突(闭集规则)。"""
        # 与东边 x=1 相切的竖直线段
        self.assertTrue(geometry.segment_intersects_polygon((1.0, 0.2), (1.0, 0.8), self.rings))
        # 恰好擦过顶点 (1,1) 的斜线
        self.assertTrue(geometry.segment_intersects_polygon((0.5, 1.5), (1.5, 0.5), self.rings))

    def test_segment_along_boundary_counts_as_conflict(self):
        """压线:航迹与边界共线重叠即判冲突。"""
        self.assertTrue(geometry.segment_intersects_polygon((0.2, 0.0), (0.8, 0.0), self.rings))

    def test_endpoint_on_boundary_counts_as_conflict(self):
        self.assertTrue(geometry.segment_intersects_polygon((0.5, 0.0), (2.0, 2.0), self.rings))

    def test_far_segment_is_clear(self):
        self.assertFalse(geometry.segment_intersects_polygon((2.0, 2.0), (3.0, 3.0), self.rings))

    def test_degenerate_segment_does_not_match_everything(self):
        """零长度段(点)只能按点判定,不能"接触"所有边(回归测试)。"""
        far = (5.0, 5.0)
        self.assertFalse(geometry.segment_intersects_polygon(far, far, self.rings))
        inside = (0.5, 0.5)
        self.assertTrue(geometry.segment_intersects_polygon(inside, inside, self.rings))


class PathAndDistanceTest(unittest.TestCase):
    def setUp(self):
        self.polygons = geometry.normalize_polygons(SQUARE)

    def test_path_intersection(self):
        path = [(2.0, 0.5), (1.5, 0.5), (0.5, 0.5)]
        self.assertTrue(geometry.path_intersects_polygons(path, self.polygons))

    def test_single_point_path(self):
        self.assertTrue(geometry.path_intersects_polygons([(0.5, 0.5)], self.polygons))
        self.assertFalse(geometry.path_intersects_polygons([(5.0, 5.0)], self.polygons))

    def test_distance_zero_when_intersecting(self):
        self.assertEqual(geometry.track_to_polygons_distance_m([(0.5, 0.5)], self.polygons), 0.0)

    def test_distance_positive_when_clear(self):
        d = geometry.track_to_polygons_distance_m([(2.0, 0.5)], self.polygons)
        self.assertGreater(d, 100000)  # 1° 经度约 111km
        self.assertLess(d, 120000)

    def test_haversine_known_value(self):
        # 赤道上 1° 经度 ≈ 111.195km
        d = geometry.haversine_m((0.0, 0.0), (1.0, 0.0))
        self.assertAlmostEqual(d, 111195, delta=500)


if __name__ == "__main__":
    unittest.main()
