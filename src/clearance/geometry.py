"""平面几何谓词与距离:点/折线与 GeoJSON 多边形的相交、相切与缓冲距离。

稳定规则(见 docs/clearance.md):
- 多边形视为闭集——边界(含顶点、边)属于区域。航迹与区域边界
  相切、压线、擦过顶点,一律判定为相交(冲突),宁可保守也不漏判。
- 判定不依赖浮点 epsilon 的偶然性:所有谓词先显式检测接触,
  再做穿越判定;相同输入永远得到相同输出(纯函数)。
- 洞(内环)按开集处理:洞的内部不属于区域,洞的边界属于区域
  (对禁飞区取保守方向)。
"""
from __future__ import annotations

import math

EPS = 1e-9
EARTH_RADIUS_M = 6371000.0


# ---------------------------------------------------------------- 基础谓词

def _open_ring(ring):
    """规范化线性环:转 float 二元组并去掉首尾重复点。"""
    pts = [(float(p[0]), float(p[1])) for p in ring]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def point_on_segment(p, a, b, eps=EPS):
    """点 p 是否在线段 ab 上(含端点);ab 退化为点时按点重合判定。"""
    seg_sq = (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2
    if seg_sq <= eps * eps:
        return (p[0] - a[0]) ** 2 + (p[1] - a[1]) ** 2 <= eps * eps
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    scale = max(1.0, abs(b[0] - a[0]), abs(b[1] - a[1]))
    if abs(cross) > eps * scale * scale:
        return False
    dot = (p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])
    if dot < -eps:
        return False
    return dot - seg_sq <= eps


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(p1, p2, p3, p4, eps=EPS):
    """线段相交判定,含端点接触、顶点相切与共线重叠(闭集规则)。"""
    d1 = _orient(p3, p4, p1)
    d2 = _orient(p3, p4, p2)
    d3 = _orient(p1, p2, p3)
    d4 = _orient(p1, p2, p4)
    if ((d1 > eps and d2 < -eps) or (d1 < -eps and d2 > eps)) and (
        (d3 > eps and d4 < -eps) or (d3 < -eps and d4 > eps)
    ):
        return True
    if abs(d1) <= eps and point_on_segment(p1, p3, p4, eps):
        return True
    if abs(d2) <= eps and point_on_segment(p2, p3, p4, eps):
        return True
    if abs(d3) <= eps and point_on_segment(p3, p1, p2, eps):
        return True
    if abs(d4) <= eps and point_on_segment(p4, p1, p2, eps):
        return True
    return False


def _ray_cast(pt, pts):
    """射线法(不含边界处理,调用方需先判边界)。"""
    inside = False
    x, y = pt
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def point_in_ring(pt, ring):
    """点是否在环内或环边界上(闭集)。"""
    pts = _open_ring(ring)
    for i in range(len(pts)):
        if point_on_segment(pt, pts[i], pts[(i + 1) % len(pts)]):
            return True
    return _ray_cast(pt, pts)


def point_in_ring_interior(pt, ring):
    """点是否严格在环内部(边界不算)。"""
    pts = _open_ring(ring)
    for i in range(len(pts)):
        if point_on_segment(pt, pts[i], pts[(i + 1) % len(pts)]):
            return False
    return _ray_cast(pt, pts)


# ---------------------------------------------------------------- GeoJSON 适配

def normalize_polygons(geometry):
    """把 GeoJSON Polygon/MultiPolygon 规范化为多边形列表(每个多边形是环列表)。"""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon":
        polygons = [[_open_ring(r) for r in coords]]
    elif gtype == "MultiPolygon":
        polygons = [[_open_ring(r) for r in poly] for poly in coords]
    else:
        raise ValueError(f"不支持的空域几何类型: {gtype!r}")
    for polygon in polygons:
        for ring in polygon:
            if len(ring) < 3:
                raise ValueError("多边形环至少需要 3 个不同顶点")
    return polygons


def track_points(geometry):
    """把段几何(Point/LineString)规范化为点列表。"""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Point":
        return [(float(coords[0]), float(coords[1]))]
    if gtype == "LineString":
        return [(float(p[0]), float(p[1])) for p in coords]
    raise ValueError(f"不支持的航迹几何类型: {gtype!r}")


def point_in_polygon(pt, rings):
    """点是否在多边形内:外环闭集内且不在任何洞的内部(洞边界算在区域内)。"""
    if not point_in_ring(pt, rings[0]):
        return False
    for hole in rings[1:]:
        if point_in_ring_interior(pt, hole):
            return False
    return True


def segment_intersects_polygon(p1, p2, rings):
    """线段是否与多边形(闭集)相交:端点在区域内,或与外环任一边相交(含相切)。"""
    if point_in_polygon(p1, rings) or point_in_polygon(p2, rings):
        return True
    ext = rings[0]
    for i in range(len(ext)):
        if segments_intersect(p1, p2, ext[i], ext[(i + 1) % len(ext)]):
            return True
    return False


def path_intersects_polygons(points, polygons):
    """折线(或单点)是否与任一多边形相交。"""
    if len(points) == 1:
        return any(point_in_polygon(points[0], rings) for rings in polygons)
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        for rings in polygons:
            if segment_intersects_polygon(a, b, rings):
                return True
    return False


# ---------------------------------------------------------------- 距离(米)

def haversine_m(a, b):
    """两 (lon, lat) 点间大圆距离,米。"""
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = math.radians(b[0] - a[0])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def path_length_m(points):
    return sum(haversine_m(points[i], points[i + 1]) for i in range(len(points) - 1))


def _projector(lat0):
    """以 lat0 为中心的等距圆柱近似投影,城市尺度下误差可忽略。"""
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110540.0
    return lambda p: (p[0] * kx, p[1] * ky)


def _point_seg_dist_xy(p, a, b):
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _seg_seg_dist_xy(p1, p2, p3, p4):
    if segments_intersect(p1, p2, p3, p4):
        return 0.0
    return min(
        _point_seg_dist_xy(p1, p3, p4),
        _point_seg_dist_xy(p2, p3, p4),
        _point_seg_dist_xy(p3, p1, p2),
        _point_seg_dist_xy(p4, p1, p2),
    )


def track_to_polygons_distance_m(points, polygons):
    """航迹到多边形集合的最短水平距离(米);相交时为 0。"""
    if path_intersects_polygons(points, polygons):
        return 0.0
    lat0 = sum(p[1] for p in points) / len(points)
    proj = _projector(lat0)
    track = [proj(p) for p in points]
    if len(track) == 1:
        track = [track[0], track[0]]
    best = math.inf
    for rings in polygons:
        ext = [proj(p) for p in rings[0]]
        for i in range(len(ext)):
            edge_a, edge_b = ext[i], ext[(i + 1) % len(ext)]
            for j in range(len(track) - 1):
                d = _seg_seg_dist_xy(track[j], track[j + 1], edge_a, edge_b)
                if d < best:
                    best = d
    return best
