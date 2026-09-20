"""业务编排:版本化数据写入、放行评估、通告生命周期、复核与追溯。

关键规则(见 docs/clearance.md):
- 空域/通告按 (id, version) 不可变存储;撤回是追加事件,不改写历史。
- 同一任务的并发提交只产生一个有效版本:幂等键唯一 + 乐观版本检查,
  全部在单事务内完成,冲突返回 409。
- 新通告影响已放行计划时,只生成待复核任务与提醒,绝不改写原决定。
- 所有决定固化当时采用的空域、通告、规则参数与人工意见(快照),
  任何结论都可追溯完整判断过程。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

from . import geometry, rules, timeutil


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _loads(text):
    return json.loads(text)


def _new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class ClearanceService:
    def __init__(self, store, clock=time.time):
        self.store = store
        self.clock = clock

    # ------------------------------------------------------------ 审计

    def _audit(self, conn, actor, action, entity_type, entity_id, detail=None):
        conn.execute(
            "INSERT INTO audit_events(actor, action, entity_type, entity_id, detail_json, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, _dumps(detail) if detail is not None else None, self.clock()),
        )

    # ------------------------------------------------------------ 规则参数

    def put_ruleset(self, version, params, actor="system"):
        now = self.clock()
        with self.store.transaction() as conn:
            existing = conn.execute(
                "SELECT params_json FROM rulesets WHERE version=?", (version,)
            ).fetchone()
            if existing:
                if _loads(existing["params_json"]) == params:
                    return {"version": version, "params": params, "existed": True}
                raise ApiError(409, "ruleset_version_conflict", f"规则版本 {version} 已存在且内容不同")
            conn.execute(
                "INSERT INTO rulesets(version, params_json, created_at) VALUES (?,?,?)",
                (version, _dumps(params), now),
            )
            self._audit(conn, actor, "ruleset.put", "ruleset", version, params)
        return {"version": version, "params": params, "existed": False}

    def current_ruleset(self):
        row = self.store.query_one(
            "SELECT version, params_json FROM rulesets ORDER BY created_at DESC, version DESC LIMIT 1"
        )
        if row:
            params = _loads(row["params_json"])
            params.setdefault("version", row["version"])
            return params
        return dict(rules.DEFAULT_RULESET)

    # ------------------------------------------------------------ 引用数据(机型/操作员)

    def put_aircraft_model(self, model_id, payload, actor="system"):
        return self._put_reference("aircraft_models", "model_id", model_id, payload, actor)

    def put_operator(self, operator_id, payload, actor="system"):
        return self._put_reference("operators", "operator_id", operator_id, payload, actor)

    def _put_reference(self, table, key, rid, payload, actor):
        now = self.clock()
        with self.store.transaction() as conn:
            conn.execute(
                f"INSERT INTO {table}({key}, payload_json, created_at) VALUES (?,?,?)"
                f" ON CONFLICT({key}) DO UPDATE SET payload_json=excluded.payload_json",
                (rid, _dumps(payload), now),
            )
            self._audit(conn, actor, f"{table}.put", table, rid, payload)
        return {key: rid, "payload": payload}

    def _get_reference(self, table, key, rid):
        row = self.store.query_one(f"SELECT payload_json FROM {table} WHERE {key}=?", (rid,))
        return _loads(row["payload_json"]) if row else None

    # ------------------------------------------------------------ 空域(版本化)

    def put_zone(self, zone_id, payload, actor="system"):
        try:
            rules.validate_zone_payload(payload)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ApiError(422, "invalid_zone", str(exc))
        version = payload.get("version")
        if not isinstance(version, int) or version < 1:
            raise ApiError(422, "invalid_zone", "version 必须是从 1 开始的整数")
        now = self.clock()
        with self.store.transaction() as conn:
            latest = conn.execute(
                "SELECT MAX(version) AS v FROM zones WHERE zone_id=?", (zone_id,)
            ).fetchone()["v"]
            existing = conn.execute(
                "SELECT * FROM zones WHERE zone_id=? AND version=?", (zone_id, version)
            ).fetchone()
            if existing:
                if self._zone_row_matches(existing, payload):
                    return self._zone_response(existing, existed=True)
                raise ApiError(409, "zone_version_conflict", f"空域 {zone_id} 版本 {version} 已存在且内容不同")
            if version != (latest or 0) + 1:
                raise ApiError(
                    409, "zone_version_conflict",
                    f"空域 {zone_id} 下一版本应为 {(latest or 0) + 1},收到 {version}",
                )
            conn.execute(
                "INSERT INTO zones(zone_id, version, kind, name, geometry_json, floor_m, ceiling_m,"
                " active_from, active_to, daily_windows_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    zone_id, version, payload["kind"], payload.get("name"),
                    _dumps(payload["geometry"]), payload.get("floor_m"), payload.get("ceiling_m"),
                    payload.get("active_from"), payload.get("active_to"),
                    _dumps(payload["daily_windows"]) if payload.get("daily_windows") else None,
                    now,
                ),
            )
            self._audit(conn, actor, "zone.put", "zone", f"{zone_id}@v{version}", payload)
        return self._zone_response(
            self.store.query_one("SELECT * FROM zones WHERE zone_id=? AND version=?", (zone_id, version)),
            existed=False,
        )

    @staticmethod
    def _zone_row_matches(row, payload):
        return (
            row["kind"] == payload["kind"]
            and _loads(row["geometry_json"]) == payload["geometry"]
            and row["floor_m"] == payload.get("floor_m")
            and row["ceiling_m"] == payload.get("ceiling_m")
            and row["active_from"] == payload.get("active_from")
            and row["active_to"] == payload.get("active_to")
            and (_loads(row["daily_windows_json"]) if row["daily_windows_json"] else None)
            == payload.get("daily_windows")
        )

    @staticmethod
    def _zone_response(row, existed):
        return {
            "zone_id": row["zone_id"],
            "version": row["version"],
            "kind": row["kind"],
            "name": row["name"],
            "geometry": _loads(row["geometry_json"]),
            "floor_m": row["floor_m"],
            "ceiling_m": row["ceiling_m"],
            "active_from": row["active_from"],
            "active_to": row["active_to"],
            "daily_windows": _loads(row["daily_windows_json"]) if row["daily_windows_json"] else None,
            "existed": existed,
        }

    def list_zones(self):
        rows = self.store.query(
            "SELECT z.* FROM zones z JOIN (SELECT zone_id, MAX(version) v FROM zones GROUP BY zone_id)"
            " latest ON z.zone_id=latest.zone_id AND z.version=latest.v ORDER BY z.zone_id"
        )
        return [self._zone_response(r, existed=True) for r in rows]

    # ------------------------------------------------------------ 通告(版本化 + 撤回)

    def put_notice(self, notice_id, payload, actor="system"):
        try:
            rules.validate_notice_payload(payload)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ApiError(422, "invalid_notice", str(exc))
        version = payload.get("version")
        if not isinstance(version, int) or version < 1:
            raise ApiError(422, "invalid_notice", "version 必须是从 1 开始的整数")
        now = self.clock()
        job_id = None
        with self.store.transaction() as conn:
            latest = conn.execute(
                "SELECT MAX(version) AS v FROM notices WHERE notice_id=?", (notice_id,)
            ).fetchone()["v"]
            existing = conn.execute(
                "SELECT * FROM notices WHERE notice_id=? AND version=?", (notice_id, version)
            ).fetchone()
            if existing:
                if self._notice_row_matches(existing, payload):
                    return self._notice_response(existing, existed=True)
                raise ApiError(409, "notice_version_conflict", f"通告 {notice_id} 版本 {version} 已存在且内容不同")
            if version != (latest or 0) + 1:
                raise ApiError(
                    409, "notice_version_conflict",
                    f"通告 {notice_id} 下一版本应为 {(latest or 0) + 1},收到 {version}",
                )
            withdrawn = conn.execute(
                "SELECT 1 FROM notice_withdrawals WHERE notice_id=?", (notice_id,)
            ).fetchone()
            if withdrawn:
                raise ApiError(409, "notice_withdrawn", f"通告 {notice_id} 已撤回,不能发布新版本")
            conn.execute(
                "INSERT INTO notices(notice_id, version, kind, title, geometry_json, floor_m, ceiling_m,"
                " effective_from, effective_to, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    notice_id, version, payload["kind"], payload.get("title"),
                    _dumps(payload["geometry"]), payload.get("floor_m"), payload.get("ceiling_m"),
                    payload["effective_from"], payload.get("effective_to"), payload.get("reason"),
                    now,
                ),
            )
            # 定时生效工作:到点扫描对已放行计划的影响,生成待复核与提醒。
            due = timeutil.parse_instant(payload["effective_from"]).timestamp()
            job_id = _new_id("job")
            conn.execute(
                "INSERT INTO scheduled_jobs(job_id, kind, payload_json, due_at, status, created_at)"
                " VALUES (?,?,?,?, 'pending', ?)",
                (job_id, "notice_effective", _dumps({"notice_id": notice_id, "version": version}), due, now),
            )
            self._audit(conn, actor, "notice.put", "notice", f"{notice_id}@v{version}", payload)
        # 已到生效时刻的通告立即处理(未来生效的由调度器/重启恢复处理)。
        self.run_due_jobs()
        return self._notice_response(
            self.store.query_one(
                "SELECT * FROM notices WHERE notice_id=? AND version=?", (notice_id, version)
            ),
            existed=False,
        )

    @staticmethod
    def _notice_row_matches(row, payload):
        return (
            row["kind"] == payload["kind"]
            and _loads(row["geometry_json"]) == payload["geometry"]
            and row["floor_m"] == payload.get("floor_m")
            and row["ceiling_m"] == payload.get("ceiling_m")
            and row["effective_from"] == payload["effective_from"]
            and row["effective_to"] == payload.get("effective_to")
        )

    def _notice_response(self, row, existed):
        withdrawn = self.store.query_one(
            "SELECT withdrawn_at, reason, actor FROM notice_withdrawals WHERE notice_id=?",
            (row["notice_id"],),
        )
        return {
            "notice_id": row["notice_id"],
            "version": row["version"],
            "kind": row["kind"],
            "title": row["title"],
            "geometry": _loads(row["geometry_json"]),
            "floor_m": row["floor_m"],
            "ceiling_m": row["ceiling_m"],
            "effective_from": row["effective_from"],
            "effective_to": row["effective_to"],
            "reason": row["reason"],
            "withdrawn": withdrawn is not None,
            "withdrawal": dict(withdrawn) if withdrawn else None,
            "existed": existed,
        }

    def list_notices(self):
        rows = self.store.query(
            "SELECT n.* FROM notices n JOIN (SELECT notice_id, MAX(version) v FROM notices GROUP BY notice_id)"
            " latest ON n.notice_id=latest.notice_id AND n.version=latest.v ORDER BY n.notice_id"
        )
        return [self._notice_response(r, existed=True) for r in rows]

    def withdraw_notice(self, notice_id, actor="system", reason=None, withdrawn_at=None):
        now = self.clock()
        when = timeutil.iso_utc(timeutil.parse_instant(withdrawn_at)) if withdrawn_at else timeutil.iso_utc(
            timeutil.parse_instant(now)
        )
        with self.store.transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM notice_withdrawals WHERE notice_id=?", (notice_id,)
            ).fetchone()
            if existing:
                raise ApiError(409, "notice_already_withdrawn", f"通告 {notice_id} 已撤回")
            latest = conn.execute(
                "SELECT MAX(version) AS v FROM notices WHERE notice_id=?", (notice_id,)
            ).fetchone()["v"]
            if latest is None:
                raise ApiError(404, "notice_not_found", f"通告 {notice_id} 不存在")
            conn.execute(
                "INSERT INTO notice_withdrawals(notice_id, withdrawn_at, reason, actor, created_at)"
                " VALUES (?,?,?,?,?)",
                (notice_id, when, reason, actor, now),
            )
            self._audit(conn, actor, "notice.withdraw", "notice", notice_id, {"reason": reason, "at": when})
            affected = self._decisions_citing_notice(conn, notice_id)
            for decision in affected:
                reminder_id = _new_id("rem")
                conn.execute(
                    "INSERT INTO reminders(reminder_id, kind, mission_id, decision_id, notice_id,"
                    " message, due_at, status, created_at) VALUES (?,?,?,?,?,?,?, 'pending', ?)",
                    (
                        reminder_id, "notice_withdrawn", decision["mission_id"], decision["decision_id"],
                        notice_id,
                        f"通告 {notice_id} 已撤回;决定 {decision['decision_id']} 曾引用该通告,"
                        f"原决定保持不变,可提交新计划版本申请重新评估。",
                        now, now,
                    ),
                )
                self._audit(
                    conn, actor, "reminder.raise", "reminder", reminder_id,
                    {"kind": "notice_withdrawn", "decision_id": decision["decision_id"]},
                )
        return {"notice_id": notice_id, "withdrawn": True, "withdrawn_at": when, "reminders_raised": len(affected)}

    @staticmethod
    def _decisions_citing_notice(conn, notice_id):
        """找出结论理由中引用过该通告的决定(用于撤回提醒)。"""
        rows = conn.execute("SELECT decision_id, mission_id, result_json FROM decisions").fetchall()
        found = []
        for row in rows:
            result = _loads(row["result_json"])
            refs = [r for sr in result["segment_results"] for r in sr["reasons"]]
            refs += result["mission_reasons"]
            if any(r.get("notice_id") == notice_id for r in refs):
                found.append({"decision_id": row["decision_id"], "mission_id": row["mission_id"]})
        return found

    # ------------------------------------------------------------ 任务提交与放行评估

    def submit_mission(self, payload, actor="system"):
        mission_id = payload.get("mission_id")
        idem_key = payload.get("idempotency_key")
        if not mission_id or not idem_key:
            raise ApiError(422, "invalid_mission", "mission_id 与 idempotency_key 必填")
        plan = payload.get("plan") or {}
        try:
            rules.validate_plan(plan)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ApiError(422, "invalid_plan", str(exc))
        model = self._get_reference("aircraft_models", "model_id", plan.get("aircraft_model_id"))
        if model is None:
            raise ApiError(422, "invalid_plan", f"机型 {plan.get('aircraft_model_id')!r} 未登记")
        operator = self._get_reference("operators", "operator_id", plan.get("operator_id"))
        if operator is None:
            raise ApiError(422, "invalid_plan", f"操作员 {plan.get('operator_id')!r} 未登记")

        now = self.clock()
        with self.store.transaction() as conn:
            # 幂等重放:相同幂等键直接返回首次提交的结果。
            replay = conn.execute(
                "SELECT mission_id, plan_version, decision_id FROM mission_plans WHERE idempotency_key=?",
                (idem_key,),
            ).fetchone()
            if replay:
                if replay["mission_id"] != mission_id:
                    raise ApiError(409, "idempotency_key_conflict", "幂等键已被其他任务使用")
                return self._submission_response(conn, replay["mission_id"], replay["plan_version"], replay["decision_id"], replayed=True)

            latest = conn.execute(
                "SELECT MAX(plan_version) AS v FROM mission_plans WHERE mission_id=?", (mission_id,)
            ).fetchone()["v"] or 0
            expected_base = payload.get("expected_base_version", 0)
            if expected_base != latest:
                raise ApiError(
                    409, "plan_version_conflict",
                    f"任务 {mission_id} 当前版本为 {latest},与 expected_base_version={expected_base} 冲突",
                )
            plan_version = latest + 1
            try:
                conn.execute(
                    "INSERT INTO mission_plans(mission_id, plan_version, idempotency_key, payload_json, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (mission_id, plan_version, idem_key, _dumps(payload), now),
                )
            except sqlite3.IntegrityError as exc:
                # 并发下同 (mission_id, plan_version) 或同幂等键只有一个事务成功。
                raise ApiError(409, "plan_version_conflict", f"任务 {mission_id} 存在并发提交,请刷新后重试") from exc

            zones = self._applicable_zones(conn)
            notices = self._applicable_notices(conn)
            ruleset = self.current_ruleset()
            result = rules.evaluate_plan(plan, zones, notices, model, operator, ruleset, timeutil.parse_instant(now))
            decision_id = _new_id("dec")
            snapshot = {
                "zones": zones,
                "notices": notices,
                "ruleset": ruleset,
                "aircraft_model": {"model_id": plan.get("aircraft_model_id"), **model},
                "operator": {"operator_id": plan.get("operator_id"), **operator},
                "human_notes": payload.get("human_notes") or [],
                "plan": plan,
            }
            revision_of = None
            if plan_version > 1:
                prev = conn.execute(
                    "SELECT decision_id FROM mission_plans WHERE mission_id=? AND plan_version=?",
                    (mission_id, plan_version - 1),
                ).fetchone()
                revision_of = prev["decision_id"] if prev else None
            conn.execute(
                "INSERT INTO decisions(decision_id, mission_id, plan_version, verdict, ruleset_version,"
                " snapshot_json, result_json, revision_of, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    decision_id, mission_id, plan_version, result["verdict"],
                    ruleset.get("version", "unknown"), _dumps(snapshot), _dumps(result),
                    revision_of, now,
                ),
            )
            conn.execute(
                "UPDATE mission_plans SET decision_id=? WHERE mission_id=? AND plan_version=?",
                (decision_id, mission_id, plan_version),
            )
            self._audit(
                conn, actor, "mission.submit", "mission", mission_id,
                {"plan_version": plan_version, "decision_id": decision_id, "verdict": result["verdict"]},
            )
            return self._submission_response(conn, mission_id, plan_version, decision_id, replayed=False)

    def _submission_response(self, conn, mission_id, plan_version, decision_id, replayed):
        decision = conn.execute(
            "SELECT verdict, result_json, created_at FROM decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        return {
            "mission_id": mission_id,
            "plan_version": plan_version,
            "decision_id": decision_id,
            "verdict": decision["verdict"],
            "result": _loads(decision["result_json"]),
            "replayed": replayed,
        }

    @staticmethod
    def _applicable_zones(conn):
        rows = conn.execute(
            "SELECT z.* FROM zones z JOIN (SELECT zone_id, MAX(version) v FROM zones GROUP BY zone_id)"
            " latest ON z.zone_id=latest.zone_id AND z.version=latest.v"
        ).fetchall()
        return [
            {
                "zone_id": r["zone_id"], "version": r["version"], "kind": r["kind"], "name": r["name"],
                "geometry": _loads(r["geometry_json"]), "floor_m": r["floor_m"], "ceiling_m": r["ceiling_m"],
                "active_from": r["active_from"], "active_to": r["active_to"],
                "daily_windows": _loads(r["daily_windows_json"]) if r["daily_windows_json"] else None,
            }
            for r in rows
        ]

    @staticmethod
    def _applicable_notices(conn):
        """评估时刻适用的通告:每个 notice_id 的最新版本且未撤回。"""
        rows = conn.execute(
            "SELECT n.* FROM notices n"
            " JOIN (SELECT notice_id, MAX(version) v FROM notices GROUP BY notice_id) latest"
            "   ON n.notice_id=latest.notice_id AND n.version=latest.v"
            " LEFT JOIN notice_withdrawals w ON w.notice_id=n.notice_id"
            " WHERE w.notice_id IS NULL"
        ).fetchall()
        return [
            {
                "notice_id": r["notice_id"], "version": r["version"], "kind": r["kind"], "title": r["title"],
                "geometry": _loads(r["geometry_json"]), "floor_m": r["floor_m"], "ceiling_m": r["ceiling_m"],
                "effective_from": r["effective_from"], "effective_to": r["effective_to"], "reason": r["reason"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------ 查询与追溯

    def get_mission(self, mission_id):
        plans = self.store.query(
            "SELECT plan_version, decision_id, created_at FROM mission_plans WHERE mission_id=? ORDER BY plan_version",
            (mission_id,),
        )
        if not plans:
            raise ApiError(404, "mission_not_found", f"任务 {mission_id} 不存在")
        latest = self.store.query_one(
            "SELECT payload_json FROM mission_plans WHERE mission_id=? ORDER BY plan_version DESC LIMIT 1",
            (mission_id,),
        )
        return {
            "mission_id": mission_id,
            "plans": [dict(p) for p in plans],
            "latest_plan": _loads(latest["payload_json"]),
        }

    def get_decision(self, decision_id):
        row = self.store.query_one("SELECT * FROM decisions WHERE decision_id=?", (decision_id,))
        if not row:
            raise ApiError(404, "decision_not_found", f"决定 {decision_id} 不存在")
        return self._decision_response(row)

    @staticmethod
    def _decision_response(row):
        return {
            "decision_id": row["decision_id"],
            "mission_id": row["mission_id"],
            "plan_version": row["plan_version"],
            "verdict": row["verdict"],
            "ruleset_version": row["ruleset_version"],
            "revision_of": row["revision_of"],
            "result": _loads(row["result_json"]),
            "created_at": row["created_at"],
        }

    def get_trace(self, decision_id):
        """从任一结论追溯完整判断过程:决定、快照、人工意见、审计链、关联复核。"""
        row = self.store.query_one("SELECT * FROM decisions WHERE decision_id=?", (decision_id,))
        if not row:
            raise ApiError(404, "decision_not_found", f"决定 {decision_id} 不存在")
        annotations = self.store.query(
            "SELECT actor, note, created_at FROM annotations WHERE decision_id=? ORDER BY annotation_id",
            (decision_id,),
        )
        events = self.store.query(
            "SELECT event_id, actor, action, entity_type, entity_id, detail_json, created_at"
            " FROM audit_events WHERE entity_id IN (?, ?) OR entity_id LIKE ? ORDER BY event_id",
            (decision_id, row["mission_id"], f"{row['mission_id']}%"),
        )
        reviews = self.store.query(
            "SELECT review_id, notice_id, notice_version, status, reason, created_at"
            " FROM review_tasks WHERE decision_id=?",
            (decision_id,),
        )
        return {
            **self._decision_response(row),
            "snapshot": _loads(row["snapshot_json"]),
            "annotations": [dict(a) for a in annotations],
            "audit_events": [
                {**{k: e[k] for k in ("event_id", "actor", "action", "entity_type", "entity_id", "created_at")},
                 "detail": _loads(e["detail_json"]) if e["detail_json"] else None}
                for e in events
            ],
            "review_tasks": [dict(r) for r in reviews],
        }

    def add_annotation(self, decision_id, actor, note):
        if not note:
            raise ApiError(422, "invalid_annotation", "note 必填")
        now = self.clock()
        with self.store.transaction() as conn:
            decision = conn.execute("SELECT 1 FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
            if not decision:
                raise ApiError(404, "decision_not_found", f"决定 {decision_id} 不存在")
            cur = conn.execute(
                "INSERT INTO annotations(decision_id, actor, note, created_at) VALUES (?,?,?,?)",
                (decision_id, actor, note, now),
            )
            self._audit(conn, actor, "decision.annotate", "decision", decision_id, {"note": note})
            return {"annotation_id": cur.lastrowid, "decision_id": decision_id}

    # ------------------------------------------------------------ 复核与提醒

    def list_reviews(self, status=None):
        sql = "SELECT * FROM review_tasks"
        args = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        rows = self.store.query(sql + " ORDER BY created_at", args)
        return [dict(r) for r in rows]

    def _transition_review(self, review_id, actor, action, note, to_status, from_statuses):
        now = self.clock()
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM review_tasks WHERE review_id=?", (review_id,)).fetchone()
            if not row:
                raise ApiError(404, "review_not_found", f"复核任务 {review_id} 不存在")
            if row["status"] not in from_statuses:
                raise ApiError(409, "review_state_conflict", f"复核任务状态为 {row['status']},不能执行 {action}")
            if to_status == "acknowledged":
                conn.execute(
                    "UPDATE review_tasks SET status=?, acked_at=?, acked_by=? WHERE review_id=?",
                    (to_status, now, actor, review_id),
                )
            else:
                conn.execute(
                    "UPDATE review_tasks SET status=?, resolved_at=?, resolved_by=?, resolution=? WHERE review_id=?",
                    (to_status, now, actor, note, review_id),
                )
            self._audit(conn, actor, f"review.{action}", "review", review_id, {"note": note})
        return dict(self.store.query_one("SELECT * FROM review_tasks WHERE review_id=?", (review_id,)))

    def ack_review(self, review_id, actor="system", note=None):
        return self._transition_review(review_id, actor, "ack", note, "acknowledged", ("pending",))

    def resolve_review(self, review_id, actor="system", note=None):
        return self._transition_review(review_id, actor, "resolve", note, "resolved", ("pending", "acknowledged"))

    def list_reminders(self, status="pending", now=None):
        now = self.clock() if now is None else now
        return [
            dict(r)
            for r in self.store.query(
                "SELECT * FROM reminders WHERE status=? AND due_at<=? ORDER BY due_at", (status, now)
            )
        ]

    # ------------------------------------------------------------ 定时生效工作与重启恢复

    def recover(self):
        """服务启动时调用:立即补跑到期的定时工作。未完成的复核任务本就
        持久化在库中,值班人员通过 GET /reviews 继续处理。"""
        return self.run_due_jobs()

    def run_due_jobs(self, now=None):
        now = self.clock() if now is None else now
        due = self.store.query(
            "SELECT * FROM scheduled_jobs WHERE status='pending' AND due_at<=? ORDER BY due_at", (now,)
        )
        done = 0
        for job in due:
            with self.store.transaction() as conn:
                still = conn.execute(
                    "SELECT status FROM scheduled_jobs WHERE job_id=?", (job["job_id"],)
                ).fetchone()
                if not still or still["status"] != "pending":
                    continue
                payload = _loads(job["payload_json"])
                if job["kind"] == "notice_effective":
                    self._handle_notice_effective(conn, payload, now)
                conn.execute(
                    "UPDATE scheduled_jobs SET status='done', done_at=? WHERE job_id=?",
                    (now, job["job_id"]),
                )
                done += 1
        return done

    def _handle_notice_effective(self, conn, payload, now):
        notice_id, version = payload["notice_id"], payload["version"]
        row = conn.execute(
            "SELECT * FROM notices WHERE notice_id=? AND version=?", (notice_id, version)
        ).fetchone()
        withdrawn = conn.execute(
            "SELECT 1 FROM notice_withdrawals WHERE notice_id=?", (notice_id,)
        ).fetchone()
        if not row or withdrawn:
            # 生效前被撤回(或记录缺失):跳过影响扫描,只留审计。
            self._audit(conn, "system", "job.notice_effective.skip", "notice", notice_id,
                        {"version": version, "cause": "withdrawn_or_missing"})
            return
        notice = {
            "notice_id": row["notice_id"], "version": row["version"], "kind": row["kind"],
            "title": row["title"], "geometry": _loads(row["geometry_json"]),
            "floor_m": row["floor_m"], "ceiling_m": row["ceiling_m"],
            "effective_from": row["effective_from"], "effective_to": row["effective_to"],
        }
        created = self._scan_notice_impact(conn, notice, now)
        self._audit(conn, "system", "job.notice_effective", "notice", notice_id,
                    {"version": version, "reviews_created": created})

    def _scan_notice_impact(self, conn, notice, now):
        """新通告生效:找出受影响的已放行决定,只生成待复核任务与提醒。

        绝不改写原决定;复核结论由人工通过新计划版本(新决定)表达。
        """
        notice["_polygons"] = geometry.normalize_polygons(notice["geometry"])
        rows = conn.execute(
            "SELECT d.decision_id, d.mission_id, d.plan_version, mp.payload_json FROM decisions d"
            " JOIN mission_plans mp ON mp.mission_id=d.mission_id AND mp.plan_version=d.plan_version"
            " WHERE d.verdict IN ('APPROVED','CONDITIONALLY_APPROVED')"
            "   AND NOT EXISTS (SELECT 1 FROM mission_plans newer"
            "                   WHERE newer.mission_id=d.mission_id AND newer.plan_version>d.plan_version)"
        ).fetchall()
        created = 0
        for row in rows:
            plan = _loads(row["payload_json"]).get("plan") or {}
            try:
                segments = rules.validate_plan(plan)
            except (ValueError, TypeError, AttributeError):
                continue
            hit_segments = [
                seg["segment_id"]
                for seg in segments
                if rules.notice_time_overlaps(notice, seg["start"], seg["end"])
                and rules.segment_conflicts_region(seg, notice)
            ]
            if not hit_segments:
                continue
            review_id = _new_id("rev")
            reason = (
                f"通告 {notice['notice_id']}@v{notice['version']} 生效后与任务 "
                f"{row['mission_id']} 的段 {', '.join(hit_segments)} 冲突,需人工复核"
            )
            try:
                conn.execute(
                    "INSERT INTO review_tasks(review_id, decision_id, mission_id, notice_id,"
                    " notice_version, status, reason, created_at) VALUES (?,?,?,?,?, 'pending', ?, ?)",
                    (review_id, row["decision_id"], row["mission_id"], notice["notice_id"],
                     notice["version"], reason, now),
                )
            except sqlite3.IntegrityError:
                continue  # 同一决定+通告版本只生成一次
            reminder_id = _new_id("rem")
            conn.execute(
                "INSERT INTO reminders(reminder_id, kind, mission_id, decision_id, review_id, notice_id,"
                " message, due_at, status, created_at) VALUES (?,?,?,?,?,?,?,?, 'pending', ?)",
                (
                    reminder_id, "review_pending", row["mission_id"], row["decision_id"], review_id,
                    notice["notice_id"],
                    f"已放行任务 {row['mission_id']} 受新通告 {notice['notice_id']} 影响,"
                    f"已生成待复核 {review_id};原决定 {row['decision_id']} 保持不变。",
                    now, now,
                ),
            )
            self._audit(conn, "system", "review.raise", "review", review_id,
                        {"decision_id": row["decision_id"], "notice_id": notice["notice_id"]})
            created += 1
        return created
