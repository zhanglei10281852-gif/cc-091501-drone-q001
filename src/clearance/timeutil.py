"""时间工具:统一 UTC 比较、半开区间、跨午夜每日窗口展开。

稳定规则(见 docs/clearance.md):
- 所有瞬时时间解析为 UTC 后比较;不带时区的输入按默认时区
  (Asia/Shanghai, 固定 UTC+8,无夏令时)解释。
- 所有时间窗口均为半开区间 [start, end):start 命中算在窗内,
  end 命中算在窗外。因此任务段结束时刻恰好等于通告生效时刻
  不冲突,段开始时刻恰好等于通告失效时刻也不冲突,边界时刻确定。
- 跨午夜不需要特判:绝对时间轴天然连续;每日重复窗口(如
  "每日 22:00-次日 06:00")按本地日期展开成绝对区间再求交。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

UTC = timezone.utc
# 业务默认时区:Asia/Shanghai 为固定 UTC+8,不依赖主机 tzdata。
DEFAULT_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

# 开放时间窗的哨兵值(不用极大 epoch 秒,避免超出 datetime 范围)。
EPOCH_START = datetime.min.replace(tzinfo=UTC)
FAR_FUTURE = datetime.max.replace(tzinfo=UTC)


def parse_instant(value, default_tz=DEFAULT_TZ):
    """把 ISO 8601 字符串/epoch 秒/datetime 解析为 UTC aware datetime。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"无效的时间格式: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(UTC)


def iso_utc(dt):
    """格式化为带 Z 的 UTC ISO 字符串,用于存储与展示。"""
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_hhmm(text):
    """把 'HH:MM' 或 'HH:MM:SS' 解析为自当日零点的偏移。"""
    parts = str(text).strip().split(":")
    if len(parts) < 2:
        raise ValueError(f"无效的时间点: {text!r}")
    hours, minutes = int(parts[0]), int(parts[1])
    seconds = int(parts[2]) if len(parts) > 2 else 0
    if not (0 <= hours <= 23 and 0 <= minutes <= 59 and 0 <= seconds <= 59):
        raise ValueError(f"无效的时间点: {text!r}")
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def overlaps(a_start, a_end, b_start, b_end):
    """半开区间相交判定:[a_start,a_end) 与 [b_start,b_end) 是否有交集。"""
    return a_start < b_end and b_start < a_end


def expand_daily_windows(windows, range_start, range_end, tz=DEFAULT_TZ):
    """把每日重复窗口展开为与 [range_start, range_end) 相交的 UTC 绝对区间列表。

    窗口形如 {"start": "22:00", "end": "06:00"},按 tz 的本地日期解释;
    end <= start 时自动跨到次日(跨午夜)。返回 [(start, end), ...](UTC)。
    """
    results = []
    if not windows:
        return results
    first_day = range_start.astimezone(tz).date() - timedelta(days=1)
    last_day = range_end.astimezone(tz).date() + timedelta(days=1)
    day = first_day
    while day <= last_day:
        base = datetime.combine(day, time.min, tzinfo=tz)
        for window in windows:
            w_start = base + parse_hhmm(window["start"])
            w_end = base + parse_hhmm(window["end"])
            if w_end <= w_start:
                w_end += timedelta(days=1)
            w_start_utc = w_start.astimezone(UTC)
            w_end_utc = w_end.astimezone(UTC)
            if overlaps(w_start_utc, w_end_utc, range_start, range_end):
                results.append((w_start_utc, w_end_utc))
        day += timedelta(days=1)
    return results
