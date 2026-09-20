"""平面几何：GeoJSON 经纬度按平面近似处理（低空小范围运行足够精确）。

稳定规则：空域按闭集处理——边界接触（含相切、共线压边）即视为进入，
同样的输入永远得到同样的结果。EPS 仅用于浮点归并，不改变闭集语义。
"""
from __future__ import annotations

import math

EPS = 1e-9

EARTH_RADIUS_M = 6_371_000.0


def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _lerp(p, q, t):
    return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)


def normalize_ring(ring):
    """去掉 GeoJSON 闭合环的重复末点，统一为浮点坐标序列。"""
    pts = [(float(pt[0]), float(pt[1])) for pt in ring]
    while len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def point_on_segment(p, a, b):
    if abs(_cross(a, b, p)) > EPS:
        return False
    return (
        min(a[0], b[0]) - EPS <= p[0] <= max(a[0], b[0]) + EPS
        and min(a[1], b[1]) - EPS <= p[1] <= max(a[1], b[1]) + EPS
    )


def _on_ring_boundary(p, ring):
    n = len(ring)
    return any(point_on_segment(p, ring[i], ring[(i + 1) % n]) for i in range(n))


def _ray_cast(p, ring):
    inside = False
    n = len(ring)
    for i in range(n):
        a = ring[i]
        b = ring[(i + 1) % n]
        if (a[1] > p[1]) != (b[1] > p[1]):
            x = a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if x > p[0]:
                inside = not inside
    return inside


def point_in_polygon(p, rings):
    """闭集语义：位于任一环边界上（含洞的边界）都视为在多边形内。"""
    if not rings:
        return False
    for ring in rings:
        if _on_ring_boundary(p, ring):
            return True
    if not _ray_cast(p, rings[0]):
        return False
    return not any(_ray_cast(p, hole) for hole in rings[1:])


def _segment_meet_params(p, q, c, d):
    """线段 pq 与 cd 的交点在 pq 上的参数 t 列表，闭区间语义。"""
    rx, ry = q[0] - p[0], q[1] - p[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    denom = rx * sy - ry * sx
    if abs(denom) <= EPS:
        # 平行或共线
        if abs(_cross(p, q, c)) > EPS:
            return []
        if rx * rx + ry * ry <= EPS:
            # pq 退化为点
            return [0.0] if point_on_segment(p, c, d) else []
        dd = rx * rx + ry * ry
        t0 = ((c[0] - p[0]) * rx + (c[1] - p[1]) * ry) / dd
        t1 = ((d[0] - p[0]) * rx + (d[1] - p[1]) * ry) / dd
        lo, hi = max(0.0, min(t0, t1)), min(1.0, max(t0, t1))
        if lo > hi + EPS:
            return []
        lo = min(1.0, max(0.0, lo))
        hi = min(1.0, max(0.0, hi))
        return [lo] if hi - lo <= EPS else [lo, hi]
    t = ((c[0] - p[0]) * sy - (c[1] - p[1]) * sx) / denom
    u = ((c[0] - p[0]) * ry - (c[1] - p[1]) * rx) / denom
    if -EPS <= t <= 1.0 + EPS and -EPS <= u <= 1.0 + EPS:
        return [min(1.0, max(0.0, t))]
    return []


def segment_polygon_intervals(p, q, rings):
    """线段 pq 落在闭多边形内的参数区间列表 [(t0, t1), ...]（按 t 升序）。

    相切或触边但未穿入时返回退化区间 (t, t)，保证闭集规则下
    "边界相切即冲突" 的判定稳定。
    """
    p = (float(p[0]), float(p[1]))
    q = (float(q[0]), float(q[1]))
    ts = {0.0, 1.0}
    for ring in rings:
        n = len(ring)
        for i in range(n):
            for t in _segment_meet_params(p, q, ring[i], ring[(i + 1) % n]):
                ts.add(t)
    ordered = []
    for t in sorted(ts):
        if not ordered or t - ordered[-1] > EPS:
            ordered.append(t)
    intervals = []
    for a, b in zip(ordered, ordered[1:]):
        if b - a <= EPS:
            continue
        if point_in_polygon(_lerp(p, q, (a + b) / 2.0), rings):
            intervals.append((a, b))
    # 触边但未穿入的相切点：补退化区间
    for t in ordered:
        if any(a - EPS <= t <= b + EPS for a, b in intervals):
            continue
        if point_in_polygon(_lerp(p, q, t), rings):
            intervals.append((t, t))
    intervals.sort()
    return intervals


def haversine_m(a, b):
    """两点 (lon, lat) 球面距离，米。"""
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))
