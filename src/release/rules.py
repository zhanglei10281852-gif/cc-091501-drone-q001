"""放行评估引擎：把空域、通告、机型能力、操作员资质放到同一次判断中。

输入均为 service 层预处理好的结构（时间已解析为带时区 datetime，
几何已解析为环序列）。输出结构化结论：整体裁决、逐段结果、
拒绝理由与附加条件，全部可落盘追溯。
"""
from __future__ import annotations

from datetime import timedelta

from .geometry import haversine_m, segment_polygon_intervals
from .timeutil import daily_windows_overlap, intervals_overlap, iso_utc

RULESET_VERSION = "release-rules/1.0.0"

SEGMENT_TYPES = {"takeoff", "enroute", "hover", "alternate", "return", "landing"}

SEGMENT_LABELS = {
    "takeoff": "起飞",
    "enroute": "航路",
    "hover": "悬停",
    "alternate": "备降",
    "return": "返航",
    "landing": "降落",
}

ZONE_KINDS = {"no_fly", "restricted"}
NOTICE_KINDS = {"no_fly", "altitude_limit"}

VERDICT_ORDER = {"approved": 0, "conditional": 1, "rejected": 2}


def _worst(verdicts):
    result = "approved"
    for verdict in verdicts:
        if VERDICT_ORDER[verdict] > VERDICT_ORDER[result]:
            result = verdict
    return result


def _finding(rule, severity, code, message, **details):
    return {
        "rule": rule,
        "severity": severity,  # reject | condition
        "code": code,
        "message": message,
        "details": details,
    }


def segment_legs(segment):
    """把分段规范化为航段列表 [(p0, p1), ...]；单点分段退化为零长度航段。"""
    points = segment["points"]
    if segment["type"] == "hover" and len(points) == 1:
        duration = float(segment.get("duration_s") or 0)
        end = dict(points[0])
        end["time"] = points[0]["time"] + timedelta(seconds=duration)
        points = [points[0], end]
    if len(points) == 1:
        return [(points[0], points[0])]
    return [(points[i], points[i + 1]) for i in range(len(points) - 1)]


def _lerp_value(v0, v1, t):
    return v0 + (v1 - v0) * t


def _zone_active(zone, t0, t1, tz_name):
    if not intervals_overlap(t0, t1, zone.get("active_from"), zone.get("active_until")):
        return False
    windows = zone.get("daily_windows")
    if windows and not daily_windows_overlap(t0, t1, windows, tz_name):
        return False
    return True


def _volume_hits(legs, rings_list, lower_m, upper_m, active_fn):
    """航段与若干闭合棱柱（水平环 + 垂直区间 + 时间窗）的冲突命中列表。"""
    hits = []
    for p0, p1 in legs:
        a2 = (p0["lon"], p0["lat"])
        b2 = (p1["lon"], p1["lat"])
        for rings in rings_list:
            for t0, t1 in segment_polygon_intervals(a2, b2, rings):
                alt0 = _lerp_value(p0["alt_m"], p1["alt_m"], t0)
                alt1 = _lerp_value(p0["alt_m"], p1["alt_m"], t1)
                alt_lo, alt_hi = min(alt0, alt1), max(alt0, alt1)
                if lower_m is not None and alt_hi < lower_m:
                    continue
                if upper_m is not None and alt_lo > upper_m:
                    continue
                time0 = p0["time"] + (p1["time"] - p0["time"]) * t0
                time1 = p0["time"] + (p1["time"] - p0["time"]) * t1
                time_lo, time_hi = min(time0, time1), max(time0, time1)
                if not active_fn(time_lo, time_hi):
                    continue
                hits.append(
                    {
                        "alt_range": [alt_lo, alt_hi],
                        "time_range": [time_lo, time_hi],
                    }
                )
    return hits


def _zone_findings(segment, legs, zone, tz_name):
    label = SEGMENT_LABELS[segment["type"]]
    hits = _volume_hits(
        legs,
        zone["rings_list"],
        zone.get("lower_m"),
        zone.get("upper_m"),
        lambda t0, t1: _zone_active(zone, t0, t1, tz_name),
    )
    findings = []
    ref = {"zone_id": zone["id"], "zone_version": zone["version"]}
    for hit in hits:
        where = {
            **ref,
            "alt_range_m": hit["alt_range"],
            "time_range": [iso_utc(hit["time_range"][0]), iso_utc(hit["time_range"][1])],
        }
        if zone["kind"] == "no_fly":
            findings.append(
                _finding(
                    "airspace.no_fly",
                    "reject",
                    "ZONE_NO_FLY",
                    f"{label}段进入禁飞区 {zone['id']}"
                    f"（高度 {hit['alt_range'][0]:.0f}~{hit['alt_range'][1]:.0f} 米）",
                    **where,
                )
            )
        else:  # restricted
            cap = zone.get("max_altitude_m")
            if cap is not None and hit["alt_range"][1] > cap:
                findings.append(
                    _finding(
                        "airspace.restricted",
                        "reject",
                        "ZONE_ALT_EXCEEDED",
                        f"{label}段在限飞区 {zone['id']} 内计划高度 "
                        f"{hit['alt_range'][1]:.0f} 米超过上限 {cap:.0f} 米",
                        max_altitude_m=cap,
                        **where,
                    )
                )
            else:
                findings.append(
                    _finding(
                        "airspace.restricted",
                        "condition",
                        "ZONE_RESTRICTED",
                        f"{label}段穿越限飞区 {zone['id']}，"
                        f"须保持高度不超过 {cap:.0f} 米" if cap is not None else
                        f"{label}段穿越限飞区 {zone['id']}，须遵守该区限制",
                        max_altitude_m=cap,
                        **where,
                    )
                )
    return findings


def _notice_findings(segment, legs, notice):
    label = SEGMENT_LABELS[segment["type"]]
    hits = _volume_hits(
        legs,
        notice["rings_list"],
        None,
        None,
        lambda t0, t1: intervals_overlap(
            t0, t1, notice.get("effective_from"), notice.get("effective_until")
        ),
    )
    findings = []
    ref = {"notice_id": notice["id"], "notice_version": notice["version"]}
    for hit in hits:
        where = {
            **ref,
            "alt_range_m": hit["alt_range"],
            "time_range": [iso_utc(hit["time_range"][0]), iso_utc(hit["time_range"][1])],
        }
        if notice["kind"] == "no_fly":
            findings.append(
                _finding(
                    "notice.no_fly",
                    "reject",
                    "NOTICE_NO_FLY",
                    f"{label}段在通告 {notice['id']}（第{notice['version']}版）"
                    "生效期间进入其禁飞范围",
                    **where,
                )
            )
        else:  # altitude_limit
            cap = notice.get("max_altitude_m")
            if cap is not None and hit["alt_range"][1] > cap:
                findings.append(
                    _finding(
                        "notice.altitude_limit",
                        "reject",
                        "NOTICE_ALT_EXCEEDED",
                        f"{label}段在通告 {notice['id']} 限高区内计划高度 "
                        f"{hit['alt_range'][1]:.0f} 米超过 {cap:.0f} 米",
                        max_altitude_m=cap,
                        **where,
                    )
                )
            else:
                findings.append(
                    _finding(
                        "notice.altitude_limit",
                        "condition",
                        "NOTICE_ALT_LIMIT",
                        f"{label}段经过通告 {notice['id']} 限高区，"
                        f"须保持高度不超过 {cap:.0f} 米",
                        max_altitude_m=cap,
                        **where,
                    )
                )
    return findings


def _aircraft_findings(segment, aircraft):
    findings = []
    label = SEGMENT_LABELS[segment["type"]]
    max_alt = aircraft.get("max_altitude_m")
    if max_alt is not None:
        top = max(p["alt_m"] for p in segment["points"])
        if top > max_alt:
            findings.append(
                _finding(
                    "aircraft.altitude",
                    "reject",
                    "AIRCRAFT_ALTITUDE",
                    f"{label}段计划高度 {top:.0f} 米超过机型升限 {max_alt:.0f} 米",
                    max_altitude_m=max_alt,
                )
            )
    if segment["type"] == "hover" and aircraft.get("hover_capable") is False:
        findings.append(
            _finding(
                "aircraft.hover",
                "reject",
                "AIRCRAFT_HOVER_UNSUPPORTED",
                f"机型不支持悬停，无法执行{label}段",
            )
        )
    return findings


def _operator_segment_findings(segment, operator, legs):
    valid_until = operator.get("valid_until")
    if valid_until is None:
        return []
    # 用航段终点而非原始航点，单点悬停的持续时长才不会漏判
    end_time = max(p["time"] for leg in legs for p in leg)
    if end_time <= valid_until:
        return []
    label = SEGMENT_LABELS[segment["type"]]
    return [
        _finding(
            "operator.license",
            "reject",
            "OPERATOR_LICENSE_EXPIRED",
            f"操作员资质于 {iso_utc(valid_until)} 到期，"
            f"无法覆盖{label}段（至 {iso_utc(end_time)}）",
            valid_until=iso_utc(valid_until),
        )
    ]


def _mission_findings(plan):
    findings = []
    operator = plan["operator"]
    aircraft = plan["aircraft"]
    categories = operator.get("categories")
    if categories is not None and plan["category"] not in categories:
        findings.append(
            _finding(
                "operator.category",
                "reject",
                "OPERATOR_CATEGORY",
                f"操作员资质不覆盖任务类别 {plan['category']}",
                category=plan["category"],
            )
        )
    legs = [leg for seg in plan["segments"] for leg in segment_legs(seg)]
    max_range = aircraft.get("max_range_m")
    if max_range is not None:
        total = sum(
            haversine_m((p0["lon"], p0["lat"]), (p1["lon"], p1["lat"])) for p0, p1 in legs
        )
        if total > max_range:
            findings.append(
                _finding(
                    "aircraft.range",
                    "reject",
                    "AIRCRAFT_RANGE",
                    f"航线总长 {total:.0f} 米超过机型航程 {max_range:.0f} 米",
                    total_range_m=total,
                    max_range_m=max_range,
                )
            )
    max_time = aircraft.get("max_flight_time_s")
    if max_time is not None:
        times = [p["time"] for seg in plan["segments"] for p in seg["points"]]
        if plan["segments"]:
            for seg in plan["segments"]:
                if seg["type"] == "hover" and len(seg["points"]) == 1:
                    times.append(
                        seg["points"][0]["time"]
                        + timedelta(seconds=float(seg.get("duration_s") or 0))
                    )
        span = (max(times) - min(times)).total_seconds() if times else 0.0
        if span > max_time:
            findings.append(
                _finding(
                    "aircraft.endurance",
                    "reject",
                    "AIRCRAFT_ENDURANCE",
                    f"任务历时 {span:.0f} 秒超过机型续航 {max_time:.0f} 秒",
                    span_s=span,
                    max_flight_time_s=max_time,
                )
            )
    return findings


def _segment_verdict(findings):
    if any(f["severity"] == "reject" for f in findings):
        return "rejected"
    if any(f["severity"] == "condition" for f in findings):
        return "conditional"
    return "approved"


def _dedup(findings):
    seen = set()
    result = []
    for f in findings:
        key = (f["code"], tuple(sorted((k, str(v)) for k, v in f["details"].items())))
        if key in seen:
            continue
        seen.add(key)
        result.append(f)
    return result


def evaluate(plan, zones, notices, tz_name):
    """完整放行评估。plan/zones/notices 均为预处理结构。"""
    segments_out = []
    all_findings = []
    for seg in plan["segments"]:
        legs = segment_legs(seg)
        findings = []
        for zone in zones:
            findings.extend(_zone_findings(seg, legs, zone, tz_name))
        for notice in notices:
            findings.extend(_notice_findings(seg, legs, notice))
        findings.extend(_aircraft_findings(seg, plan["aircraft"]))
        findings.extend(_operator_segment_findings(seg, plan["operator"], legs))
        segments_out.append(
            {
                "id": seg["id"],
                "type": seg["type"],
                "label": SEGMENT_LABELS[seg["type"]],
                "verdict": _segment_verdict(findings),
                "findings": findings,
            }
        )
        all_findings.extend(findings)
    mission_findings = _mission_findings(plan)
    verdict = _worst(
        [s["verdict"] for s in segments_out]
        + [_segment_verdict(mission_findings)]
    )
    return {
        "verdict": verdict,
        "segments": segments_out,
        "mission_findings": mission_findings,
        "reasons": _dedup(
            [f for f in all_findings + mission_findings if f["severity"] == "reject"]
        ),
        "conditions": _dedup([f for f in all_findings if f["severity"] == "condition"]),
    }


def notice_conflicts(plan, notice, tz_name):
    """计划与单条通告的实质冲突（用于新通告影响已放行计划的扫描）。

    只返回拒绝级命中：禁飞通告的任何进入、限高通告的实际超高，
    才算"影响"已放行计划。
    """
    conflicts = []
    for seg in plan["segments"]:
        legs = segment_legs(seg)
        for finding in _notice_findings(seg, legs, notice):
            if finding["severity"] == "reject":
                conflicts.append(
                    {
                        "segment_id": seg["id"],
                        "segment_label": SEGMENT_LABELS[seg["type"]],
                        **finding,
                    }
                )
    return conflicts
