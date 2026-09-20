"""SQLite 持久化层:单连接 + 可重入锁,事务边界清晰,重启后状态完整保留。

数据库路径由运行时配置指定(环境变量 DRONE_OPS_DB 或显式 config),
不在代码里隐藏任何主机状态;测试可用 ":memory:" 或临时目录。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS rulesets (
    version      TEXT PRIMARY KEY,
    params_json  TEXT NOT NULL,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS zones (
    zone_id            TEXT NOT NULL,
    version            INTEGER NOT NULL,
    kind               TEXT NOT NULL,
    name               TEXT,
    geometry_json      TEXT NOT NULL,
    floor_m            REAL,
    ceiling_m          REAL,
    active_from        TEXT,
    active_to          TEXT,
    daily_windows_json TEXT,
    created_at         REAL NOT NULL,
    PRIMARY KEY (zone_id, version)
);

CREATE TABLE IF NOT EXISTS notices (
    notice_id       TEXT NOT NULL,
    version         INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    title           TEXT,
    geometry_json   TEXT NOT NULL,
    floor_m         REAL,
    ceiling_m       REAL,
    effective_from  TEXT NOT NULL,
    effective_to    TEXT,
    reason          TEXT,
    created_at      REAL NOT NULL,
    PRIMARY KEY (notice_id, version)
);

-- 撤回是独立事件,不改写通告历史版本。
CREATE TABLE IF NOT EXISTS notice_withdrawals (
    notice_id    TEXT PRIMARY KEY,
    withdrawn_at TEXT NOT NULL,
    reason       TEXT,
    actor        TEXT,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS aircraft_models (
    model_id     TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS operators (
    operator_id  TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at   REAL NOT NULL
);

-- 同一任务的计划版本:(mission_id, plan_version) 主键 + 幂等键唯一,
-- 保证并发提交只产生一个有效版本。
CREATE TABLE IF NOT EXISTS mission_plans (
    mission_id      TEXT NOT NULL,
    plan_version    INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json    TEXT NOT NULL,
    decision_id     TEXT,
    created_at      REAL NOT NULL,
    PRIMARY KEY (mission_id, plan_version)
);

-- 决定不可变:verdict、当时采用的空域/通告/规则/人工意见全部固化在快照里。
CREATE TABLE IF NOT EXISTS decisions (
    decision_id     TEXT PRIMARY KEY,
    mission_id      TEXT NOT NULL,
    plan_version    INTEGER NOT NULL,
    verdict         TEXT NOT NULL,
    ruleset_version TEXT NOT NULL,
    snapshot_json   TEXT NOT NULL,
    result_json     TEXT NOT NULL,
    revision_of     TEXT,
    created_at      REAL NOT NULL
);

-- 人工意见:追加式记录,不回写决定本身。
CREATE TABLE IF NOT EXISTS annotations (
    annotation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id   TEXT NOT NULL,
    actor         TEXT NOT NULL,
    note          TEXT NOT NULL,
    created_at    REAL NOT NULL
);

-- 新通告影响已放行计划时生成的待复核清单;原决定保持不变。
CREATE TABLE IF NOT EXISTS review_tasks (
    review_id      TEXT PRIMARY KEY,
    decision_id    TEXT NOT NULL,
    mission_id     TEXT NOT NULL,
    notice_id      TEXT NOT NULL,
    notice_version INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    reason         TEXT,
    created_at     REAL NOT NULL,
    acked_at       REAL,
    acked_by       TEXT,
    resolved_at    REAL,
    resolved_by    TEXT,
    resolution     TEXT,
    UNIQUE (decision_id, notice_id, notice_version)
);

CREATE TABLE IF NOT EXISTS reminders (
    reminder_id TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    mission_id  TEXT,
    decision_id TEXT,
    review_id   TEXT,
    notice_id   TEXT,
    message     TEXT NOT NULL,
    due_at      REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  REAL NOT NULL
);

-- 定时生效工作(如通告到点生效扫描):重启后扫描 pending 记录续跑。
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    job_id       TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    due_at       REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   REAL NOT NULL,
    done_at      REAL
);

-- 全量审计:任何状态变化都可追溯。
CREATE TABLE IF NOT EXISTS audit_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    detail_json TEXT,
    created_at  REAL NOT NULL
);
"""


class Store:
    """线程安全的 SQLite 存取。所有写操作走 transaction()。"""

    def __init__(self, path):
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        """单写者事务:BEGIN IMMEDIATE,异常回滚。锁保证并发请求串行进入。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def query(self, sql, args=()):
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def query_one(self, sql, args=()):
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def close(self):
        with self._lock:
            self._conn.close()
