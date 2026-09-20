"""放行规则引擎:对同一次提交的空域、通告、机型、资质做分段评估。

输入全部为"评估时刻适用"的版本化数据(由 service 层取出并固化进快照),
评估本身是纯函数:相同输入永远得到相同结论,保证边界情形规则稳定。

结论分级:
- 段级:CLEAR / CONDITIONAL / VIOLATION
- 整体:APPROVED / CONDITIONALLY_APPROVED / REJECTED
  任一段 VIOLATION 或任务级违规 → REJECTED;
  无违规但存在 CONDITIONAL → CONDITIONALLY_APPROVED(附条件);
  全部 CLEAR → APPROVED。
"""
from __future__ import annotations

from . import geometry, timeutil

# 段级结论
CLEAR = "CLEAR"
CONDITIONAL = "CONDITIONAL"
VIOLATION = "VIOLATION"

# 整体结论
APPROVED = "APPROVED"
CONDITIONALLY_APPROVED = "CONDITIONALLY_APPROVED"
REJECTED = "REJECTED"

# 内置规则参数;可通过 PUT /rulesets/{version} 注册新版本,决定会固化所用版本。
DEFAULT_RULESET = {
    "version": "builtin-v1",
    # 距禁飞区水平距离小于该值时附加"保持间隔"条件(米)
    "buffer_m": 500.0,
    # 巡航高度超过升限的 (1 - 该比例) 时附加"高度余量不足"条件
    "altitude_margin_ratio": 0.1,
    # 续航评估预留分钟数
    "endurance_reserve_minutes": 10.0,
    # 边界策略声明:闭集,相切即冲突(几何层强制执行,此处仅为快照可读性)
    "tangency_policy": "closed_set",
}

# 空域/通告类别 → 冲突时的段级结论
_ZONE_KIND_SEVERITY = {
    "no_fly": VIOLATION,        # 禁飞区:冲突即违规
    "restricted": CONDITIONAL,  # 限飞区:需事先协调
    "controlled": CONDITIONAL,  # 管制区:需申报
}

SEGMENT_PHASES = ("takeoff", "enroute", "hover", "alternate")


class PlanValidationError(ValueError):
    """计划结构不合法(对应 HTTP 422)。"""


# ---------------------------------------------------------------- 校验

def validate_zone_payload(payload):
    if payload.get("kind") not in _ZONE_KIND_SEVERITY and payload.get("kind") != "info":
        raise PlanValidationError(f"未知空域类别: {payload.get('kind')!r}")
    geometry.normalize_polygons(payload.get("geometry") or {})
    for window in payload.get("daily_windows") or []:
        timeutil.parse_hhmm(window.get("start", ""))
        timeutil.parse_hhmm(window.get("end", ""))
    for key in ("active_from", "active_to"):
        if payload.get(key):
            timeutil.parse_instant(payload[key])


def validate_notice_payload(payload):
    if payload.get("kind") not in _ZONE_KIND_SEVERITY:
        raise PlanValidationError(f"未知通告类别: {payload.get('kind')!r}")
    geometry.normalize_polygons(payload.get("geometry") or {})
    start = timeutil.parse_instant(payload.get("effective_from"))
    if payload.get("effective_to"):
        end = timeutil.parse_instant(payload["effective_to"])
        if end <= start:
            raise PlanValidationError("effective_to 必须晚于 effective_from")
    return start


def validate_plan(plan):
    """校验任务计划结构,返回规范化后的段列表(带解析后的时间)。"""
    segments = plan.get("segments")
    if not isinstance(segments, list) or not segments:
        raise PlanValidationError("segments 必须是非空数组")
    seen_ids = set()
    normalized = []
    for index, seg in enumerate(segments):
        seg_id = seg.get("segment_id") or f"seg-{index + 1}"
        if seg_id in seen_ids:
            raise PlanValidationError(f"segment_id 重复: {seg_id}")
        seen_ids.add(seg_id)
        phase = seg.get("phase")
        if phase not in SEGMENT_PHASES:
            raise PlanValidationError(f"段 {seg_id} 的 phase 非法: {phase!r}")
        points = geometry.track_points(seg.get("geometry") or {})
        if phase in ("takeoff", "hover") and len(points) != 1:
            raise PlanValidationError(f"段 {seg_id}({phase})几何必须是 Point")
        if phase in ("enroute", "alternate") and len(points) < 2:
            raise PlanValidationError(f"段 {seg_id}({phase})几何必须是 LineString")
        start = timeutil.parse_instant(seg.get("start"))
        end = timeutil.parse_instant(seg.get("end"))
        if end <= start:
            raise PlanValidationError(f"段 {seg_id} 的 end 必须晚于 start")
        altitude = seg.get("altitude_m")
        if not isinstance(altitude, (int, float)) or altitude < 0:
            raise PlanValidationError(f"段 {seg_id} 缺少合法的 altitude_m")
        normalized.append(
            {
                "segment_id": seg_id,
                "phase": phase,
                "points": points,
                "start": start,
                "end": end,
                "altitude_m": float(altitude),
            }
        )
    return normalized


# ---------------------------------------------------------------- 时空相交

def _altitude_overlaps(altitude_m, floor_m, ceiling_m):
    """垂直方向按闭区间相交:高度恰好等于上/下限也算冲突(保守、确定)。"""
    floor = float("-inf") if floor_m is None else float(floor_m)
    ceiling = float("inf") if ceiling_m is None else float(ceiling_m)
    return floor <= altitude_m <= ceiling


def zone_time_overlaps(zone, seg_start, seg_end):
    """空域生效时间与段窗口 [seg_start, seg_end) 是否相交。

    绝对窗(active_from/active_to,半开)与每日窗(daily_windows)为并集;
    两者都未配置时视为恒有效。
    """
    has_absolute = bool(zone.get("active_from") or zone.get("active_to"))
    has_daily = bool(zone.get("daily_windows"))
    if not has_absolute and not has_daily:
        return True
    if has_absolute:
        start = (
            timeutil.parse_instant(zone["active_from"])
            if zone.get("active_from")
            else timeutil.EPOCH_START
        )
        end = (
            timeutil.parse_instant(zone["active_to"])
            if zone.get("active_to")
            else timeutil.FAR_FUTURE
        )
        if timeutil.overlaps(seg_start, seg_end, start, end):
            return True
    if has_daily:
        if timeutil.expand_daily_windows(zone["daily_windows"], seg_start, seg_end):
            return True
    return False


def notice_time_overlaps(notice, seg_start, seg_end):
    start = timeutil.parse_instant(notice["effective_from"])
    end = (
        timeutil.parse_instant(notice["effective_to"])
        if notice.get("effective_to")
        else timeutil.FAR_FUTURE
    )
    return timeutil.overlaps(seg_start, seg_end, start, end)


def segment_conflicts_region(seg, region):
    """段与区域(空域或通告)的空间+高度冲突判定;时间由调用方判断。"""
    if not _altitude_overlaps(seg["altitude_m"], region.get("floor_m"), region.get("ceiling_m")):
        return False
    polygons = region["_polygons"]
    return geometry.path_intersects_polygons(seg["points"], polygons)


def _region_ref(region, id_key):
    return {"ref": f"{region[id_key]}@v{region['version']}"}


# ---------------------------------------------------------------- 评估

def evaluate_plan(plan, zones, notices, model, operator, ruleset, now):
    """对计划做一次完整放行评估。zones/notices 为评估时刻适用的版本化记录。

    返回 {"verdict", "segment_results", "mission_reasons", "conditions", "evaluated_at"}。
    """
    segments = validate_plan(plan)
    buffer_m = float(ruleset.get("buffer_m", DEFAULT_RULESET["buffer_m"]))
    margin_ratio = float(
        ruleset.get("altitude_margin_ratio", DEFAULT_RULESET["altitude_margin_ratio"])
    )
    reserve_min = float(
        ruleset.get("endurance_reserve_minutes", DEFAULT_RULESET["endurance_reserve_minutes"])
    )

    # 复制后再挂几何缓存,避免污染调用方用于快照的数据。
    zones = [dict(z) for z in zones]
    notices = [dict(n) for n in notices]
    for zone in zones:
        zone["_polygons"] = geometry.normalize_polygons(zone["geometry"])
    for notice in notices:
        notice["_polygons"] = geometry.normalize_polygons(notice["geometry"])

    segment_results = []
    for seg in segments:
        reasons = []
        reasons.extend(_check_airspace(seg, zones, buffer_m))
        reasons.extend(_check_notices(seg, notices, buffer_m))
        reasons.extend(_check_segment_capability(seg, model, margin_ratio))
        segment_results.append(
            {
                "segment_id": seg["segment_id"],
                "phase": seg["phase"],
                "verdict": _worst(reasons),
                "reasons": reasons,
            }
        )

    mission_reasons = []
    mission_reasons.extend(_check_mission_capability(segments, model, reserve_min))
    mission_reasons.extend(_check_operator(plan, segments, operator))

    verdict = _overall(segment_results, mission_reasons)
    conditions = _derive_conditions(segment_results, ruleset)
    return {
        "verdict": verdict,
        "segment_results": segment_results,
        "mission_reasons": mission_reasons,
        "conditions": conditions,
        "evaluated_at": timeutil.iso_utc(now),
    }


def _worst(reasons):
    if any(r["severity"] == VIOLATION for r in reasons):
        return VIOLATION
    if any(r["severity"] == CONDITIONAL for r in reasons):
        return CONDITIONAL
    return CLEAR


def _overall(segment_results, mission_reasons):
    if any(r["verdict"] == VIOLATION for r in segment_results) or any(
        r["severity"] == VIOLATION for r in mission_reasons
    ):
        return REJECTED
    if any(r["verdict"] == CONDITIONAL for r in segment_results) or any(
        r["severity"] == CONDITIONAL for r in mission_reasons
    ):
        return CONDITIONALLY_APPROVED
    return APPROVED


def _check_airspace(seg, zones, buffer_m):
    reasons = []
    for zone in zones:
        severity = _ZONE_KIND_SEVERITY.get(zone["kind"])
        if severity is None:
            continue
        if not zone_time_overlaps(zone, seg["start"], seg["end"]):
            continue
        ref = _region_ref(zone, "zone_id")
        name = zone.get("name") or zone["zone_id"]
        if segment_conflicts_region(seg, zone):
            reasons.append(
                {
                    "severity": severity,
                    "code": "AIRSPACE_NO_FLY" if severity == VIOLATION else "AIRSPACE_CONDITIONAL_ZONE",
                    "message": f"段与空域「{name}」({zone['kind']})时空冲突",
                    "zone_id": zone["zone_id"],
                    "zone_version": zone["version"],
                    **ref,
                }
            )
        elif severity == VIOLATION and _altitude_overlaps(
            seg["altitude_m"], zone.get("floor_m"), zone.get("ceiling_m")
        ):
            distance = geometry.track_to_polygons_distance_m(seg["points"], zone["_polygons"])
            if distance < buffer_m:
                reasons.append(
                    {
                        "severity": CONDITIONAL,
                        "code": "AIRSPACE_BUFFER",
                        "message": f"段距禁飞区「{name}」{distance:.0f}m,小于缓冲距离 {buffer_m:.0f}m",
                        "zone_id": zone["zone_id"],
                        "zone_version": zone["version"],
                        "distance_m": round(distance, 1),
                        **ref,
                    }
                )
    return reasons


def _check_notices(seg, notices, buffer_m):
    reasons = []
    for notice in notices:
        severity = _ZONE_KIND_SEVERITY.get(notice["kind"])
        if severity is None:
            continue
        if not notice_time_overlaps(notice, seg["start"], seg["end"]):
            continue
        ref = _region_ref(notice, "notice_id")
        title = notice.get("title") or notice["notice_id"]
        if segment_conflicts_region(seg, notice):
            reasons.append(
                {
                    "severity": severity,
                    "code": "NOTICE_NO_FLY" if severity == VIOLATION else "NOTICE_CONDITIONAL",
                    "message": f"段与通告「{title}」({notice['kind']})时空冲突",
                    "notice_id": notice["notice_id"],
                    "notice_version": notice["version"],
                    **ref,
                }
            )
        elif severity == VIOLATION and _altitude_overlaps(
            seg["altitude_m"], notice.get("floor_m"), notice.get("ceiling_m")
        ):
            distance = geometry.track_to_polygons_distance_m(seg["points"], notice["_polygons"])
            if distance < buffer_m:
                reasons.append(
                    {
                        "severity": CONDITIONAL,
                        "code": "NOTICE_BUFFER",
                        "message": f"段距通告禁飞区「{title}」{distance:.0f}m,小于缓冲距离 {buffer_m:.0f}m",
                        "notice_id": notice["notice_id"],
                        "notice_version": notice["version"],
                        "distance_m": round(distance, 1),
                        **ref,
                    }
                )
    return reasons


def _check_segment_capability(seg, model, margin_ratio):
    reasons = []
    max_alt = model.get("max_altitude_m")
    if max_alt is not None:
        if seg["altitude_m"] > max_alt:
            reasons.append(
                {
                    "severity": VIOLATION,
                    "code": "CAP_ALTITUDE",
                    "message": f"段高度 {seg['altitude_m']:.0f}m 超过机型升限 {max_alt:.0f}m",
                }
            )
        elif seg["altitude_m"] > max_alt * (1 - margin_ratio):
            reasons.append(
                {
                    "severity": CONDITIONAL,
                    "code": "CAP_ALTITUDE_MARGIN",
                    "message": f"段高度接近机型升限 {max_alt:.0f}m,余量不足",
                }
            )
    if seg["phase"] == "hover" and not model.get("supports_hover", False):
        reasons.append(
            {
                "severity": VIOLATION,
                "code": "CAP_NO_HOVER",
                "message": "机型不支持悬停,无法执行悬停段",
            }
        )
    max_speed = model.get("max_speed_mps")
    if max_speed is not None and len(seg["points"]) >= 2:
        hours = (seg["end"] - seg["start"]).total_seconds()
        if hours > 0:
            speed = geometry.path_length_m(seg["points"]) / hours
            if speed > max_speed:
                reasons.append(
                    {
                        "severity": VIOLATION,
                        "code": "CAP_SPEED",
                        "message": f"段所需速度 {speed:.1f}m/s 超过机型最大速度 {max_speed:.1f}m/s",
                    }
                )
    return reasons


def _check_mission_capability(segments, model, reserve_min):
    reasons = []
    total_length = sum(
        geometry.path_length_m(s["points"]) for s in segments if len(s["points"]) >= 2
    )
    max_range = model.get("max_range_m")
    if max_range is not None and total_length > max_range:
        reasons.append(
            {
                "severity": VIOLATION,
                "code": "CAP_RANGE",
                "message": f"总航程 {total_length:.0f}m 超过机型航程 {max_range:.0f}m",
            }
        )
    start = min(s["start"] for s in segments)
    end = max(s["end"] for s in segments)
    duration_min = (end - start).total_seconds() / 60.0
    endurance = model.get("endurance_minutes")
    if endurance is not None and duration_min + reserve_min > endurance:
        reasons.append(
            {
                "severity": VIOLATION,
                "code": "CAP_ENDURANCE",
                "message": (
                    f"任务时长 {duration_min:.0f}min 加预留 {reserve_min:.0f}min "
                    f"超过机型续航 {endurance:.0f}min"
                ),
            }
        )
    return reasons


def _check_operator(plan, segments, operator):
    reasons = []
    mission_end = max(s["end"] for s in segments)
    valid_until = operator.get("license_valid_until")
    if valid_until:
        if timeutil.parse_instant(valid_until) < mission_end:
            reasons.append(
                {
                    "severity": VIOLATION,
                    "code": "OP_LICENSE_EXPIRED",
                    "message": f"操作员执照有效期至 {valid_until},早于任务结束时刻",
                }
            )
    else:
        reasons.append(
            {
                "severity": VIOLATION,
                "code": "OP_LICENSE_EXPIRED",
                "message": "操作员缺少执照有效期信息",
            }
        )
    required_category = plan.get("aircraft_category")
    if required_category and required_category not in (operator.get("categories") or []):
        reasons.append(
            {
                "severity": VIOLATION,
                "code": "OP_CATEGORY",
                "message": f"操作员不具备机型类别 {required_category} 的资质",
            }
        )
    missing = set(plan.get("required_endorsements") or []) - set(
        operator.get("endorsements") or []
    )
    if missing:
        reasons.append(
            {
                "severity": VIOLATION,
                "code": "OP_ENDORSEMENT",
                "message": f"操作员缺少签注: {', '.join(sorted(missing))}",
            }
        )
    return reasons


def _derive_conditions(segment_results, ruleset):
    """把 CONDITIONAL 级理由汇总为可执行的附条件清单(去重)。"""
    conditions = []
    seen = set()
    buffer_m = ruleset.get("buffer_m", DEFAULT_RULESET["buffer_m"])
    for result in segment_results:
        for reason in result["reasons"]:
            if reason["severity"] != CONDITIONAL:
                continue
            code = reason["code"]
            if code in ("AIRSPACE_BUFFER", "NOTICE_BUFFER"):
                text = f"与 {reason['ref']} 保持至少 {buffer_m:.0f}m 水平间隔"
            elif code in ("AIRSPACE_CONDITIONAL_ZONE", "NOTICE_CONDITIONAL"):
                text = f"进入 {reason['ref']} 前完成协调/申报"
            elif code == "CAP_ALTITUDE_MARGIN":
                text = "控制巡航高度,保留机型升限余量"
            else:
                text = reason["message"]
            if text not in seen:
                seen.add(text)
                conditions.append(text)
    return conditions
