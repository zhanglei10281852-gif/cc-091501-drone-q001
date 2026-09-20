"""时间工具：统一使用带时区的 ISO 8601 字符串。

稳定规则：
- 飞行时间区间按闭区间 [start, end] 处理（端点时刻航空器确实在场）；
- 通告/空域的绝对生效窗口按半开区间 [from, until) 处理，
  航空器恰好处于 until 时刻时窗口已结束，恰好处于 from 时刻时窗口已生效；
- 跨午夜不需要特判：所有比较都基于带时区的绝对时刻；
- 每日时段窗（如 "22:00"-"06:00"）结束不晚于开始时按跨零点回绕处理，
  起止相等视为全天。
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

MAX_DAILY_WINDOW_SPAN_DAYS = 400


def parse_iso(value, field="time"):
    """解析带时区的 ISO 8601 字符串；朴素时间（无时区）一律拒绝。"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"{field} 必须携带时区偏移")
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 ISO 8601 字符串")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} 不是合法的 ISO 8601 时间: {value!r}") from exc
    if dt.tzinfo is None:
        raise ValueError(f"{field} 必须携带时区偏移: {value!r}")
    return dt


def iso_utc(dt):
    """统一落盘格式：UTC + Z 后缀，字符串可排序。"""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def intervals_overlap(start, end, win_from, win_until):
    """闭区间 [start, end] 与半开窗口 [win_from, win_until) 是否重叠；None 表示无界。"""
    if win_from is not None and end < win_from:
        return False
    if win_until is not None and not (start < win_until):
        return False
    return True


def parse_daily_window(text):
    """"HH:MM" -> 当日分钟数。"""
    if not isinstance(text, str) or len(text) != 5 or text[2] != ":":
        raise ValueError(f"每日时段必须是 HH:MM 格式: {text!r}")
    try:
        hour = int(text[:2])
        minute = int(text[3:])
    except ValueError as exc:
        raise ValueError(f"每日时段必须是 HH:MM 格式: {text!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"每日时段超出范围: {text!r}")
    return hour * 60 + minute


def daily_windows_overlap(start, end, windows, tz_name):
    """飞行区间 [start, end] 是否与任一每日时段窗重叠。

    windows 为 [(start_min, end_min), ...]，按 tz_name 的当地时间解释；
    end_min <= start_min 时跨零点回绕（含相等，相等视为全天）。
    """
    if not windows:
        return True
    tz = ZoneInfo(tz_name)
    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)
    day = local_start.date() - timedelta(days=1)
    last_day = local_end.date() + timedelta(days=1)
    if (last_day - day).days > MAX_DAILY_WINDOW_SPAN_DAYS:
        raise ValueError("飞行时间跨度超过每日时段窗可处理范围")
    while day <= last_day:
        for start_min, end_min in windows:
            win_start = datetime.combine(day, dtime(start_min // 60, start_min % 60), tz)
            win_end = datetime.combine(day, dtime(end_min // 60, end_min % 60), tz)
            if end_min <= start_min:
                win_end += timedelta(days=1)
            if intervals_overlap(start, end, win_start, win_end):
                return True
        day += timedelta(days=1)
    return False
