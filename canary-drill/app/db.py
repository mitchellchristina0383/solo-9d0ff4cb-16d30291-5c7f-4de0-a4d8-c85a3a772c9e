"""SQLite 连接与表结构。

- WAL 模式 + busy_timeout，保证 gunicorn 多 worker 下并发读写可用。
- 所有写操作走短事务；放量确认使用 IMMEDIATE 事务串行化（见 app.py）。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("CANARY_DB", "/data/canary.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    rollout_pct INTEGER NOT NULL DEFAULT 10,   -- 当前流量档位（10..100）
    finished   INTEGER NOT NULL DEFAULT 0      -- 到达 100% 后置 1
);

CREATE TABLE IF NOT EXISTS rounds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    seq           INTEGER NOT NULL UNIQUE,     -- 轮次序号，从 1 开始
    rollout_pct   INTEGER NOT NULL,            -- 本轮发出时的新档比例
    new_n         INTEGER NOT NULL,
    old_n         INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open', -- open/confirmed/rejected/expired
    opened_at     TEXT NOT NULL,
    deadline_at   TEXT NOT NULL,
    closed_at     TEXT,
    decision_note TEXT NOT NULL DEFAULT ''     -- 判定依据（人可读）
);

CREATE TABLE IF NOT EXISTS requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id   INTEGER NOT NULL REFERENCES rounds(id),
    version    TEXT NOT NULL CHECK (version IN ('new', 'old')),
    idx        INTEGER NOT NULL,
    label      TEXT NOT NULL,
    UNIQUE(round_id, idx)
);

-- 每笔请求以首个回执为准：request_id 唯一约束由数据库兜底，并发回填也只留一条。
CREATE TABLE IF NOT EXISTS receipts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  INTEGER NOT NULL UNIQUE REFERENCES requests(id),
    round_id    INTEGER NOT NULL REFERENCES rounds(id),
    version     TEXT NOT NULL,
    success     INTEGER NOT NULL CHECK (success IN (0, 1)),
    late        INTEGER NOT NULL DEFAULT 0,  -- 轮次关闭后才到的回执，仅留档不参与判定
    received_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_requests_round ON requests(round_id);
CREATE INDEX IF NOT EXISTS idx_receipts_round ON receipts(round_id);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_cursor(immediate: bool = False):
    """短事务上下文。immediate=True 时先抢写锁，把并发确认串行化。"""
    conn = get_conn()
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    # 目录可能由 compose 卷挂载而来，确保存在
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(id, rollout_pct, finished) VALUES (1, 10, 0)"
        )
        conn.commit()
    finally:
        conn.close()
