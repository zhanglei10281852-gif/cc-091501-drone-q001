"""SQLite 持久化：所有放行相关状态的唯一事实来源。

- 空域、通告按 (id, version) 追加，历史版本永不改写；
- 任务计划按 (mission_id, version) 唯一约束，配合 BEGIN IMMEDIATE 事务，
  相同任务的并发提交只能产生一个有效版本；
- 决策 append-only，固化当时采用的空域版本、通告版本、规则版本与人工意见；
- 待复核、提醒、定时生效事件全部落盘，重启后可继续。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS zones (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    checksum TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS notices (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    mission_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (mission_id, version)
);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    system_verdict TEXT NOT NULL,
    verdict TEXT NOT NULL,
    record TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_mission ON decisions (mission_id, seq);
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    trigger_ref TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT,
    UNIQUE (mission_id, trigger_kind, trigger_ref)
);
CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    mission_id TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scheduled_events (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    fire_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    processed_at TEXT
);
"""


class ConflictError(Exception):
    """版本/唯一性冲突：相同任务的并发提交中失败的一方。"""


class NotFoundError(Exception):
    """引用的对象不存在。"""


class Store:
    def __init__(self, path):
        self._path = str(path)
        self._lock = threading.RLock()
        self._conn = None

    def _connect(self):
        if self._conn is None:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.isolation_level = None  # 手动管理事务
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            self._conn = conn
        return self._conn

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @contextmanager
    def _tx(self):
        """写事务：BEGIN IMMEDIATE 保证检查-写入序列的原子性。"""
        with self._lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def _query(self, sql, params=()):
        with self._lock:
            return self._connect().execute(sql, params).fetchall()

    def _one(self, sql, params=()):
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ---- 空域 ----

    def add_zone(self, zone_id, payload, checksum, created_at):
        with self._tx() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM zones WHERE id = ?", (zone_id,)
            ).fetchone()
            version = (row["v"] or 0) + 1
            conn.execute(
                "INSERT INTO zones (id, version, payload, checksum, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (zone_id, version, json.dumps(payload, ensure_ascii=False), checksum, created_at),
            )
        return version

    def current_zones(self):
        return self._query(
            "SELECT z.* FROM zones z"
            " JOIN (SELECT id, MAX(version) AS v FROM zones GROUP BY id) latest"
            " ON latest.id = z.id AND latest.v = z.version"
        )

    def zone_history(self, zone_id):
        return self._query(
            "SELECT * FROM zones WHERE id = ? ORDER BY version", (zone_id,)
        )

    # ---- 通告 ----

    def add_notice(self, notice_id, payload, created_at):
        with self._tx() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM notices WHERE id = ?", (notice_id,)
            ).fetchone()
            version = (row["v"] or 0) + 1
            conn.execute(
                "INSERT INTO notices (id, version, payload, created_at) VALUES (?, ?, ?, ?)",
                (notice_id, version, json.dumps(payload, ensure_ascii=False), created_at),
            )
        return version

    def current_notices(self):
        return self._query(
            "SELECT n.* FROM notices n"
            " JOIN (SELECT id, MAX(version) AS v FROM notices GROUP BY id) latest"
            " ON latest.id = n.id AND latest.v = n.version"
        )

    def notice_history(self, notice_id):
        return self._query(
            "SELECT * FROM notices WHERE id = ? ORDER BY version", (notice_id,)
        )

    def get_notice_version(self, notice_id, version):
        return self._one(
            "SELECT * FROM notices WHERE id = ? AND version = ?", (notice_id, version)
        )

    # ---- 任务与计划 ----

    def create_mission(self, mission_id, category, plan_payload, created_at):
        """创建任务并落第 1 版计划；任务已存在时抛 ConflictError。"""
        with self._tx() as conn:
            try:
                conn.execute(
                    "INSERT INTO missions (id, category, status, created_at)"
                    " VALUES (?, ?, 'active', ?)",
                    (mission_id, category, created_at),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"任务 {mission_id} 已存在") from exc
            conn.execute(
                "INSERT INTO plans (mission_id, version, payload, created_at)"
                " VALUES (?, 1, ?, ?)",
                (mission_id, json.dumps(plan_payload, ensure_ascii=False), created_at),
            )

    def add_plan(self, mission_id, expected_version, plan_payload, created_at):
        """追加计划版本；expected_version 必须等于当前最新版 + 1。"""
        with self._tx() as conn:
            mission = conn.execute(
                "SELECT id FROM missions WHERE id = ?", (mission_id,)
            ).fetchone()
            if mission is None:
                raise NotFoundError(f"任务 {mission_id} 不存在")
            row = conn.execute(
                "SELECT MAX(version) AS v FROM plans WHERE mission_id = ?", (mission_id,)
            ).fetchone()
            current = row["v"] or 0
            if expected_version != current + 1:
                raise ConflictError(
                    f"计划版本冲突：期望第 {expected_version} 版，当前最新为第 {current} 版"
                )
            try:
                conn.execute(
                    "INSERT INTO plans (mission_id, version, payload, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        mission_id,
                        expected_version,
                        json.dumps(plan_payload, ensure_ascii=False),
                        created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"计划版本 {expected_version} 已被占用") from exc
        return expected_version

    def get_mission(self, mission_id):
        return self._one("SELECT * FROM missions WHERE id = ?", (mission_id,))

    def list_missions(self):
        return self._query("SELECT * FROM missions ORDER BY created_at, id")

    def current_plan(self, mission_id):
        return self._one(
            "SELECT * FROM plans WHERE mission_id = ? ORDER BY version DESC LIMIT 1",
            (mission_id,),
        )

    # ---- 决策（append-only） ----

    def append_decision(self, decision_id, mission_id, plan_version, record, created_at):
        with self._tx() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM decisions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
            seq = row["s"] + 1
            conn.execute(
                "INSERT INTO decisions"
                " (id, mission_id, plan_version, seq, system_verdict, verdict, record, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision_id,
                    mission_id,
                    plan_version,
                    seq,
                    record["system_verdict"],
                    record["verdict"],
                    json.dumps(record, ensure_ascii=False),
                    created_at,
                ),
            )
        return seq

    def get_decision(self, decision_id):
        return self._one("SELECT * FROM decisions WHERE id = ?", (decision_id,))

    def latest_decision(self, mission_id):
        return self._one(
            "SELECT * FROM decisions WHERE mission_id = ? ORDER BY seq DESC LIMIT 1",
            (mission_id,),
        )

    def decisions_for(self, mission_id):
        return self._query(
            "SELECT * FROM decisions WHERE mission_id = ? ORDER BY seq", (mission_id,)
        )

    def missions_with_open_release(self):
        """最新决策为批准/附条件批准的任务（新禁飞区复核扫描的对象）。"""
        return self._query(
            "SELECT m.id AS mission_id, d.id AS decision_id, d.verdict AS verdict"
            " FROM missions m"
            " JOIN decisions d ON d.mission_id = m.id"
            " WHERE d.seq = (SELECT MAX(seq) FROM decisions WHERE mission_id = m.id)"
            " AND d.verdict IN ('approved', 'conditional')"
        )

    # ---- 待复核与提醒 ----

    def create_review_with_reminder(
        self, review_id, mission_id, decision_id, trigger_kind, trigger_ref,
        reminder_id, message, created_at,
    ):
        """同事务落复核条目与提醒；同一 (任务, 触发源) 只生成一次。返回是否新建。"""
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO reviews"
                " (id, mission_id, decision_id, trigger_kind, trigger_ref, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (review_id, mission_id, decision_id, trigger_kind, trigger_ref, created_at),
            )
            if cur.rowcount == 0:
                return False
            conn.execute(
                "INSERT INTO reminders (id, review_id, mission_id, message, status, created_at)"
                " VALUES (?, ?, ?, ?, 'open', ?)",
                (reminder_id, review_id, mission_id, message, created_at),
            )
        return True

    def list_reviews(self, status=None):
        if status:
            return self._query(
                "SELECT * FROM reviews WHERE status = ? ORDER BY created_at, id", (status,)
            )
        return self._query("SELECT * FROM reviews ORDER BY created_at, id")

    def get_review(self, review_id):
        return self._one("SELECT * FROM reviews WHERE id = ?", (review_id,))

    def resolve_review(self, review_id, resolution, resolved_at):
        with self._tx() as conn:
            row = conn.execute(
                "SELECT status FROM reviews WHERE id = ?", (review_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"复核 {review_id} 不存在")
            if row["status"] != "pending":
                raise ConflictError(f"复核 {review_id} 已处理")
            conn.execute(
                "UPDATE reviews SET status = 'resolved', resolved_at = ?, resolution = ?"
                " WHERE id = ?",
                (resolved_at, json.dumps(resolution, ensure_ascii=False), review_id),
            )
            conn.execute(
                "UPDATE reminders SET status = 'closed' WHERE review_id = ?", (review_id,)
            )

    def list_reminders(self, status=None):
        if status:
            return self._query(
                "SELECT * FROM reminders WHERE status = ? ORDER BY created_at, id", (status,)
            )
        return self._query("SELECT * FROM reminders ORDER BY created_at, id")

    # ---- 定时生效事件 ----

    def schedule_event(self, event_id, kind, fire_at, payload):
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO scheduled_events"
                " (id, kind, fire_at, payload, status) VALUES (?, ?, ?, ?, 'pending')",
                (event_id, kind, fire_at, json.dumps(payload, ensure_ascii=False)),
            )

    def due_events(self, now_iso):
        return self._query(
            "SELECT * FROM scheduled_events WHERE status = 'pending' AND fire_at <= ?"
            " ORDER BY fire_at, id",
            (now_iso,),
        )

    def pending_events(self):
        return self._query(
            "SELECT * FROM scheduled_events WHERE status = 'pending' ORDER BY fire_at, id"
        )

    def mark_event_done(self, event_id, processed_at):
        with self._tx() as conn:
            conn.execute(
                "UPDATE scheduled_events SET status = 'done', processed_at = ? WHERE id = ?",
                (processed_at, event_id),
            )
