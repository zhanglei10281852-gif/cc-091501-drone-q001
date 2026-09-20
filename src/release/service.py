"""放行服务：编排空域、通告、计划、评估、复核与定时生效。

关键语义：
- 决策一旦写入永不改写；新禁飞区只产生待复核条目与提醒；
- 每次决策固化当时采用的空域版本与校验和、通告版本、规则版本、人工意见；
- 相同任务的并发提交由存储层唯一约束保证只有一个有效版本；
- 定时生效事件落盘，进程重启后继续处理。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import rules
from .geometry import normalize_ring
from .store import ConflictError, NotFoundError, Store
from .timeutil import iso_utc, parse_daily_window, parse_iso

DEFAULT_TZ = "Asia/Shanghai"

VERDICTS = ("approved", "conditional", "rejected")
ZONE_KINDS = ("no_fly", "restricted")
NOTICE_KINDS = ("no_fly", "altitude_limit")


class ValidationError(Exception):
    """输入不合法（HTTP 422）。"""


def _utcnow():
    return datetime.now(timezone.utc)


@dataclass
class ServiceConfig:
    data_dir: str = "./data"
    tz_name: str = DEFAULT_TZ
    scheduler_interval_s: float = 1.0
    scheduler_enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8000

    @classmethod
    def from_env(cls):
        return cls(
            data_dir=os.environ.get("RELEASE_DATA_DIR", "./data"),
            tz_name=os.environ.get("RELEASE_TZ", DEFAULT_TZ),
            scheduler_interval_s=float(os.environ.get("RELEASE_SCHED_INTERVAL", "1.0")),
            scheduler_enabled=os.environ.get("RELEASE_SCHEDULER", "1") != "0",
            host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", "8000")),
        )


# ---------- 输入校验与规范化 ----------

def _require(cond, message):
    if not cond:
        raise ValidationError(message)


def _require_keys(obj, keys, where):
    for key in keys:
        _require(key in obj, f"{where} 缺少字段 {key}")


def _parse_time_field(value, field_name):
    try:
        return parse_iso(value, field_name)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _parse_geometry(geometry, where):
    """GeoJSON Polygon/MultiPolygon -> [rings, ...]（每环已规范化）。"""
    _require(isinstance(geometry, dict), f"{where} 缺少 GeoJSON geometry")
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    _require(isinstance(coords, list), f"{where} geometry 缺少 coordinates")
    polygons = []
    if gtype == "Polygon":
        polygons = [coords]
    elif gtype == "MultiPolygon":
        polygons = coords
    else:
        raise ValidationError(f"{where} 仅支持 Polygon/MultiPolygon，收到 {gtype!r}")
    rings_list = []
    for rings in polygons:
        _require(isinstance(rings, list) and rings, f"{where} 多边形缺少环")
        norm = []
        for ring in rings:
            _require(
                isinstance(ring, list) and len(ring) >= 4,
                f"{where} 环至少需要 4 个坐标点",
            )
            for pt in ring:
                _require(
                    isinstance(pt, (list, tuple)) and len(pt) >= 2
                    and isinstance(pt[0], (int, float)) and isinstance(pt[1], (int, float)),
                    f"{where} 坐标必须是 [经度, 纬度] 数值对",
                )
                _require(-180 <= pt[0] <= 180 and -90 <= pt[1] <= 90,
                         f"{where} 坐标超出经纬度范围: {pt!r}")
            norm.append(normalize_ring(ring))
        rings_list.append(norm)
    return rings_list


def _opt_number(obj, key, where):
    value = obj.get(key)
    if value is not None:
        _require(isinstance(value, (int, float)), f"{where} 的 {key} 必须是数值")
    return value


def validate_zone_payload(payload):
    _require(isinstance(payload, dict), "空域必须是 JSON 对象")
    _require_keys(payload, ("id", "kind", "geometry"), "空域")
    _require(isinstance(payload["id"], str) and payload["id"], "空域 id 必须是非空字符串")
    _require(payload["kind"] in ZONE_KINDS, f"空域 kind 必须是 {ZONE_KINDS}")
    zone = {
        "id": payload["id"],
        "kind": payload["kind"],
        "name": payload.get("name"),
        "active": bool(payload.get("active", True)),
        "rings_list": _parse_geometry(payload["geometry"], "空域"),
        "lower_m": _opt_number(payload, "lower_m", "空域"),
        "upper_m": _opt_number(payload, "upper_m", "空域"),
        "max_altitude_m": _opt_number(payload, "max_altitude_m", "空域"),
        "active_from": None,
        "active_until": None,
        "daily_windows": None,
    }
    if payload["kind"] == "restricted":
        _require(
            zone["max_altitude_m"] is not None,
            "限飞区（restricted）必须给出 max_altitude_m 上限",
        )
    if payload.get("active_from") is not None:
        zone["active_from"] = _parse_time_field(payload["active_from"], "空域 active_from")
    if payload.get("active_until") is not None:
        zone["active_until"] = _parse_time_field(payload["active_until"], "空域 active_until")
    if payload.get("daily_windows") is not None:
        windows = payload["daily_windows"]
        _require(isinstance(windows, list) and windows, "daily_windows 必须是非空数组")
        parsed = []
        for item in windows:
            _require(
                isinstance(item, (list, tuple)) and len(item) == 2,
                "daily_windows 元素必须是 [开始, 结束]",
            )
            try:
                parsed.append((parse_daily_window(item[0]), parse_daily_window(item[1])))
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        zone["daily_windows"] = parsed
    return zone


def validate_notice_payload(payload):
    _require(isinstance(payload, dict), "通告必须是 JSON 对象")
    _require_keys(payload, ("id", "op"), "通告")
    _require(isinstance(payload["id"], str) and payload["id"], "通告 id 必须是非空字符串")
    op = payload["op"]
    _require(op in ("publish", "withdraw"), "通告 op 必须是 publish 或 withdraw")
    notice = {
        "id": payload["id"],
        "op": op,
        "reason": payload.get("reason"),
        "kind": None,
        "rings_list": [],
        "effective_from": None,
        "effective_until": None,
        "max_altitude_m": None,
    }
    if op == "withdraw":
        return notice
    _require_keys(payload, ("kind", "geometry", "effective_from"), "通告")
    _require(payload["kind"] in NOTICE_KINDS, f"通告 kind 必须是 {NOTICE_KINDS}")
    notice["kind"] = payload["kind"]
    notice["rings_list"] = _parse_geometry(payload["geometry"], "通告")
    notice["effective_from"] = _parse_time_field(payload["effective_from"], "通告 effective_from")
    if payload.get("effective_until") is not None:
        notice["effective_until"] = _parse_time_field(
            payload["effective_until"], "通告 effective_until"
        )
        _require(
            notice["effective_until"] > notice["effective_from"],
            "通告 effective_until 必须晚于 effective_from",
        )
    notice["max_altitude_m"] = _opt_number(payload, "max_altitude_m", "通告")
    if notice["kind"] == "altitude_limit":
        _require(
            notice["max_altitude_m"] is not None,
            "限高通告（altitude_limit）必须给出 max_altitude_m",
        )
    return notice


def _validate_point(pt, where):
    _require(isinstance(pt, dict), f"{where} 航点必须是对象")
    _require_keys(pt, ("lon", "lat", "alt_m", "time"), where)
    _require(
        isinstance(pt["lon"], (int, float)) and isinstance(pt["lat"], (int, float)),
        f"{where} 经纬度必须是数值",
    )
    _require(-180 <= pt["lon"] <= 180 and -90 <= pt["lat"] <= 90,
             f"{where} 坐标超出经纬度范围")
    _require(isinstance(pt["alt_m"], (int, float)), f"{where} alt_m 必须是数值")
    return {
        "lon": float(pt["lon"]),
        "lat": float(pt["lat"]),
        "alt_m": float(pt["alt_m"]),
        "time": _parse_time_field(pt["time"], f"{where} time"),
    }


def validate_plan_payload(payload):
    _require(isinstance(payload, dict), "计划必须是 JSON 对象")
    _require_keys(payload, ("category", "aircraft", "operator", "segments"), "计划")
    _require(isinstance(payload["category"], str) and payload["category"],
             "计划 category 必须是非空字符串")

    aircraft = payload["aircraft"]
    _require(isinstance(aircraft, dict), "aircraft 必须是对象")
    _require(isinstance(aircraft.get("model"), str) and aircraft["model"],
             "aircraft.model 必须是非空字符串")
    aircraft_out = {
        "model": aircraft["model"],
        "max_altitude_m": _opt_number(aircraft, "max_altitude_m", "aircraft"),
        "max_range_m": _opt_number(aircraft, "max_range_m", "aircraft"),
        "max_flight_time_s": _opt_number(aircraft, "max_flight_time_s", "aircraft"),
        "hover_capable": aircraft.get("hover_capable"),
    }

    operator = payload["operator"]
    _require(isinstance(operator, dict), "operator 必须是对象")
    _require(isinstance(operator.get("id"), str) and operator["id"],
             "operator.id 必须是非空字符串")
    categories = operator.get("categories")
    if categories is not None:
        _require(
            isinstance(categories, list) and all(isinstance(c, str) for c in categories),
            "operator.categories 必须是字符串数组",
        )
    operator_out = {
        "id": operator["id"],
        "categories": categories,
        "valid_until": None,
    }
    if operator.get("valid_until") is not None:
        operator_out["valid_until"] = _parse_time_field(
            operator["valid_until"], "operator.valid_until"
        )

    segments = payload["segments"]
    _require(isinstance(segments, list) and segments, "segments 必须是非空数组")
    segments_out = []
    for i, seg in enumerate(segments):
        where = f"segments[{i}]"
        _require(isinstance(seg, dict), f"{where} 必须是对象")
        _require_keys(seg, ("type", "points"), where)
        _require(
            seg["type"] in rules.SEGMENT_TYPES,
            f"{where}.type 必须是 {sorted(rules.SEGMENT_TYPES)}",
        )
        points = seg["points"]
        _require(isinstance(points, list) and points, f"{where}.points 必须是非空数组")
        pts = [_validate_point(p, where) for p in points]
        duration_s = seg.get("duration_s")
        if seg["type"] == "hover" and len(pts) == 1:
            _require(
                isinstance(duration_s, (int, float)) and duration_s >= 0,
                f"{where} 单点悬停必须给出非负 duration_s",
            )
        for a, b in zip(pts, pts[1:]):
            _require(b["time"] >= a["time"], f"{where} 航点时间必须单调不减")
        segments_out.append(
            {
                "id": seg.get("id") or f"seg-{i + 1}",
                "type": seg["type"],
                "points": pts,
                "duration_s": duration_s,
            }
        )
    return {
        "category": payload["category"],
        "aircraft": aircraft_out,
        "operator": operator_out,
        "segments": segments_out,
    }


def _checksum(payload):
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------- 服务 ----------

class ReleaseService:
    def __init__(self, config=None, clock=None):
        self.config = config or ServiceConfig.from_env()
        self._clock = clock or _utcnow
        self._store = None
        self._sched_thread = None
        self._sched_stop = threading.Event()

    # -- 基础设施 --

    @property
    def store(self):
        if self._store is None:
            data_dir = Path(self.config.data_dir)
            data_dir.mkdir(parents=True, exist_ok=True)
            self._store = Store(data_dir / "release.db")
        return self._store

    def close(self):
        self.stop_scheduler()
        if self._store is not None:
            self._store.close()
            self._store = None

    def start_scheduler(self):
        if self._sched_thread is not None:
            return
        self._sched_stop.clear()
        self._sched_thread = threading.Thread(
            target=self._sched_loop, name="release-scheduler", daemon=True
        )
        self._sched_thread.start()

    def stop_scheduler(self):
        if self._sched_thread is not None:
            self._sched_stop.set()
            self._sched_thread.join(timeout=5)
            self._sched_thread = None

    def _sched_loop(self):
        while not self._sched_stop.is_set():
            try:
                self.process_due_events()
            except Exception:  # 调度线程不允许崩溃，下一轮重试
                pass
            self._sched_stop.wait(self.config.scheduler_interval_s)

    # -- 空域 --

    def add_airspace(self, payload):
        zone = validate_zone_payload(payload)
        record = {
            "id": zone["id"],
            "kind": zone["kind"],
            "name": zone["name"],
            "active": zone["active"],
            "geometry": payload["geometry"],
            "lower_m": zone["lower_m"],
            "upper_m": zone["upper_m"],
            "max_altitude_m": zone["max_altitude_m"],
            "active_from": iso_utc(zone["active_from"]) if zone["active_from"] else None,
            "active_until": iso_utc(zone["active_until"]) if zone["active_until"] else None,
            "daily_windows": payload.get("daily_windows"),
        }
        checksum = _checksum(record)
        version = self.store.add_zone(zone["id"], record, checksum, iso_utc(self._clock()))
        return {"id": zone["id"], "version": version, "checksum": checksum}

    def list_airspaces(self):        return [self._zone_out(row) for row in self.store.current_zones()]

    def airspace_history(self, zone_id):
        rows = self.store.zone_history(zone_id)
        if not rows:
            raise NotFoundError(f"空域 {zone_id} 不存在")
        return [self._zone_out(row) for row in rows]

    @staticmethod
    def _zone_out(row):
        payload = json.loads(row["payload"])
        return {
            "id": row["id"],
            "version": row["version"],
            "checksum": row["checksum"],
            "created_at": row["created_at"],
            **payload,
        }

    # -- 通告 --

    def publish_notice(self, payload):
        notice = validate_notice_payload(payload)
        record = {
            "id": notice["id"],
            "op": notice["op"],
            "kind": notice["kind"],
            "reason": notice["reason"],
            "geometry": payload.get("geometry"),
            "effective_from": iso_utc(notice["effective_from"])
            if notice["effective_from"] else None,
            "effective_until": iso_utc(notice["effective_until"])
            if notice["effective_until"] else None,
            "max_altitude_m": notice["max_altitude_m"],
        }
        now = self._clock()
        version = self.store.add_notice(notice["id"], record, iso_utc(now))
        result = {"id": notice["id"], "version": version, "op": notice["op"]}
        if notice["op"] == "publish":
            if notice["effective_from"] <= now:
                result["affected"] = self._scan_notice(notice["id"], version)
            else:
                event_id = f"evt-{notice['id']}-v{version}-effective"
                self.store.schedule_event(
                    event_id,
                    "notice_effective",
                    iso_utc(notice["effective_from"]),
                    {"notice_id": notice["id"], "version": version},
                )
                result["scheduled_event"] = event_id
        return result

    def list_notices(self):
        return [self._notice_out(row) for row in self.store.current_notices()]

    def notice_history(self, notice_id):
        rows = self.store.notice_history(notice_id)
        if not rows:
            raise NotFoundError(f"通告 {notice_id} 不存在")
        return [self._notice_out(row) for row in rows]

    @staticmethod
    def _notice_out(row):
        payload = json.loads(row["payload"])
        return {
            "id": row["id"],
            "version": row["version"],
            "created_at": row["created_at"],
            **payload,
        }

    # -- 任务与放行决策 --

    def submit_mission(self, payload):
        _require(isinstance(payload, dict), "请求体必须是 JSON 对象")
        _require_keys(payload, ("mission_id", "plan"), "提交")
        mission_id = payload["mission_id"]
        _require(isinstance(mission_id, str) and mission_id, "mission_id 必须是非空字符串")
        plan = validate_plan_payload(payload["plan"])
        raw_plan = payload["plan"]
        now = iso_utc(self._clock())
        self.store.create_mission(mission_id, plan["category"], raw_plan, now)
        decision = self._evaluate_and_record(
            mission_id,
            decided_by=payload.get("decided_by"),
            human_note=payload.get("human_note"),
            verdict_override=payload.get("verdict_override"),
        )
        return {"mission": self.get_mission(mission_id), "decision": decision}

    def submit_plan(self, mission_id, payload):
        _require(isinstance(payload, dict), "请求体必须是 JSON 对象")
        _require_keys(payload, ("plan", "expected_version"), "提交计划")
        plan = validate_plan_payload(payload["plan"])
        expected = payload["expected_version"]
        _require(isinstance(expected, int) and expected >= 1,
                 "expected_version 必须是正整数")
        now = iso_utc(self._clock())
        version = self.store.add_plan(mission_id, expected, payload["plan"], now)
        decision = self._evaluate_and_record(
            mission_id,
            decided_by=payload.get("decided_by"),
            human_note=payload.get("human_note"),
            verdict_override=payload.get("verdict_override"),
        )
        return {"plan_version": version, "decision": decision}

    def evaluate_mission(self, mission_id, payload):
        payload = payload or {}
        return self._evaluate_and_record(
            mission_id,
            decided_by=payload.get("decided_by"),
            human_note=payload.get("human_note"),
            verdict_override=payload.get("verdict_override"),
        )

    def _evaluate_and_record(self, mission_id, decided_by, human_note, verdict_override):
        mission = self.store.get_mission(mission_id)
        if mission is None:
            raise NotFoundError(f"任务 {mission_id} 不存在")
        plan_row = self.store.current_plan(mission_id)
        plan = validate_plan_payload(json.loads(plan_row["payload"]))
        zones = self._current_zones_prepared()
        notices = self._current_notices_prepared()
        result = rules.evaluate(plan, zones, notices, self.config.tz_name)
        system_verdict = result["verdict"]
        verdict = system_verdict
        if verdict_override is not None:
            _require(verdict_override in VERDICTS,
                     f"verdict_override 必须是 {VERDICTS}")
            _require(human_note, "人工改判必须填写 human_note 说明理由")
            verdict = verdict_override
        now = iso_utc(self._clock())
        decision_id = f"dec-{uuid.uuid4().hex[:12]}"
        record = {
            "id": decision_id,
            "mission_id": mission_id,
            "plan_version": plan_row["version"],
            "system_verdict": system_verdict,
            "verdict": verdict,
            "reasons": result["reasons"],
            "conditions": result["conditions"],
            "segments": result["segments"],
            "mission_findings": result["mission_findings"],
            "snapshot": {
                "ruleset_version": rules.RULESET_VERSION,
                "airspace": [
                    {"id": z["id"], "version": z["version"], "checksum": z["checksum"]}
                    for z in zones
                ],
                "notices": [
                    {"id": n["id"], "version": n["version"]} for n in notices
                ],
                "aircraft": plan["aircraft"],
                "operator": {
                    "id": plan["operator"]["id"],
                    "categories": plan["operator"]["categories"],
                    "valid_until": iso_utc(plan["operator"]["valid_until"])
                    if plan["operator"]["valid_until"] else None,
                },
                "category": plan["category"],
                "evaluated_at": now,
            },
            "decided_by": decided_by,
            "human_note": human_note,
        }
        seq = self.store.append_decision(
            decision_id, mission_id, plan_row["version"], record, now
        )
        record["seq"] = seq
        record["created_at"] = now
        return record

    def _current_zones_prepared(self):
        zones = []
        for row in self.store.current_zones():
            payload = json.loads(row["payload"])
            if not payload.get("active", True):
                continue
            zones.append(
                {
                    "id": row["id"],
                    "version": row["version"],
                    "checksum": row["checksum"],
                    "kind": payload["kind"],
                    "rings_list": _parse_geometry(payload["geometry"], "空域"),
                    "lower_m": payload.get("lower_m"),
                    "upper_m": payload.get("upper_m"),
                    "max_altitude_m": payload.get("max_altitude_m"),
                    "active_from": parse_iso(payload["active_from"])
                    if payload.get("active_from") else None,
                    "active_until": parse_iso(payload["active_until"])
                    if payload.get("active_until") else None,
                    "daily_windows": [
                        (parse_daily_window(w[0]), parse_daily_window(w[1]))
                        for w in payload["daily_windows"]
                    ]
                    if payload.get("daily_windows") else None,
                }
            )
        return zones

    def _current_notices_prepared(self):
        notices = []
        for row in self.store.current_notices():
            payload = json.loads(row["payload"])
            if payload.get("op") != "publish":
                continue  # 已撤回的通告不参与评估
            notices.append(self._prepare_notice(payload, row["version"]))
        return notices

    @staticmethod
    def _prepare_notice(payload, version):
        return {
            "id": payload["id"],
            "version": version,
            "kind": payload["kind"],
            "rings_list": _parse_geometry(payload["geometry"], "通告"),
            "effective_from": parse_iso(payload["effective_from"]),
            "effective_until": parse_iso(payload["effective_until"])
            if payload.get("effective_until") else None,
            "max_altitude_m": payload.get("max_altitude_m"),
        }

    # -- 查询 --

    def get_mission(self, mission_id):
        mission = self.store.get_mission(mission_id)
        if mission is None:
            raise NotFoundError(f"任务 {mission_id} 不存在")
        plan_row = self.store.current_plan(mission_id)
        decision = self.store.latest_decision(mission_id)
        open_for_mission = len(
            [r for r in self.store.list_reviews("pending") if r["mission_id"] == mission_id]
        )
        return {
            "id": mission["id"],
            "category": mission["category"],
            "status": mission["status"],
            "created_at": mission["created_at"],
            "current_plan_version": plan_row["version"] if plan_row else None,
            "latest_decision": self._decision_summary(decision) if decision else None,
            "open_reviews": open_for_mission,
        }

    def list_missions(self):
        return [self.get_mission(row["id"]) for row in self.store.list_missions()]

    @staticmethod
    def _decision_summary(row):
        return {
            "id": row["id"],
            "seq": row["seq"],
            "plan_version": row["plan_version"],
            "system_verdict": row["system_verdict"],
            "verdict": row["verdict"],
            "created_at": row["created_at"],
        }

    def get_decision(self, decision_id):
        row = self.store.get_decision(decision_id)
        if row is None:
            raise NotFoundError(f"决策 {decision_id} 不存在")
        record = json.loads(row["record"])
        record["seq"] = row["seq"]
        record["created_at"] = row["created_at"]
        return record

    def list_decisions(self, mission_id):
        if self.store.get_mission(mission_id) is None:
            raise NotFoundError(f"任务 {mission_id} 不存在")
        return [self._decision_summary(row) for row in self.store.decisions_for(mission_id)]

    # -- 待复核与提醒 --

    def list_reviews(self, status=None):
        return [self._review_out(row) for row in self.store.list_reviews(status)]

    def list_reminders(self, status=None):
        rows = self.store.list_reminders(status)
        return [
            {
                "id": row["id"],
                "review_id": row["review_id"],
                "mission_id": row["mission_id"],
                "message": row["message"],
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _review_out(row):
        return {
            "id": row["id"],
            "mission_id": row["mission_id"],
            "decision_id": row["decision_id"],
            "trigger_kind": row["trigger_kind"],
            "trigger_ref": row["trigger_ref"],
            "status": row["status"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "resolution": json.loads(row["resolution"]) if row["resolution"] else None,
        }

    def resolve_review(self, review_id, payload):
        _require(isinstance(payload, dict), "请求体必须是 JSON 对象")
        resolved_by = payload.get("resolved_by")
        _require(isinstance(resolved_by, str) and resolved_by, "resolved_by 必填")
        note = payload.get("note")
        review = self.store.get_review(review_id)
        if review is None:
            raise NotFoundError(f"复核 {review_id} 不存在")
        if review["status"] != "pending":
            raise ConflictError(f"复核 {review_id} 已处理")
        resolution = {
            "resolved_by": resolved_by,
            "note": note,
            "new_decision_id": None,
        }
        if payload.get("re_evaluate"):
            decision = self._evaluate_and_record(
                review["mission_id"],
                decided_by=resolved_by,
                human_note=note or f"复核 {review_id} 触发的重新评估",
                verdict_override=None,
            )
            resolution["new_decision_id"] = decision["id"]
        self.store.resolve_review(review_id, resolution, iso_utc(self._clock()))
        return self._review_out(self.store.get_review(review_id))

    # -- 定时生效与复核扫描 --

    def process_due_events(self, at=None):
        """处理到期的定时事件；重启后调用即可继续未完成的定时生效工作。"""
        at = at or self._clock()
        processed = []
        for event in self.store.due_events(iso_utc(at)):
            payload = json.loads(event["payload"])
            if event["kind"] == "notice_effective":
                self._scan_notice(payload["notice_id"], payload["version"])
            self.store.mark_event_done(event["id"], iso_utc(at))
            processed.append(event["id"])
        return processed

    def _scan_notice(self, notice_id, version):
        """新通告影响已放行计划：只生成待复核与提醒，不改写原决策。"""
        row = self.store.get_notice_version(notice_id, version)
        if row is None:
            return 0
        payload = json.loads(row["payload"])
        if payload.get("op") != "publish":
            return 0
        notice = self._prepare_notice(payload, version)
        affected = 0
        for mission in self.store.missions_with_open_release():
            plan_row = self.store.current_plan(mission["mission_id"])
            plan = validate_plan_payload(json.loads(plan_row["payload"]))
            conflicts = rules.notice_conflicts(plan, notice, self.config.tz_name)
            if not conflicts:
                continue
            review_id = f"rev-{uuid.uuid4().hex[:12]}"
            reminder_id = f"rem-{uuid.uuid4().hex[:12]}"
            segments = "、".join(sorted({c["segment_label"] for c in conflicts}))
            message = (
                f"通告 {notice_id}（第 {version} 版）与任务 {mission['mission_id']}"
                f" 已放行计划的{segments}段冲突，请复核"
            )
            created = self.store.create_review_with_reminder(
                review_id,
                mission["mission_id"],
                mission["decision_id"],
                "notice",
                f"{notice_id}@{version}",
                reminder_id,
                message,
                iso_utc(self._clock()),
            )
            if created:
                affected += 1
        return affected
