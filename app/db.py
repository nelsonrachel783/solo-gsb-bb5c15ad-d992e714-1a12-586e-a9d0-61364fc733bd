"""SQLite 访问层：连接获取、建表初始化与写事务。"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = "data/lab.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL CHECK (kind IN ('sample', 'pool')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS pool_recipes (
    pool_id       INTEGER NOT NULL REFERENCES batches (id) ON DELETE CASCADE,
    ingredient_id INTEGER NOT NULL REFERENCES batches (id),
    portions      INTEGER NOT NULL CHECK (portions > 0),
    PRIMARY KEY (pool_id, ingredient_id)
);

CREATE TABLE IF NOT EXISTS exclusion_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_a_id INTEGER NOT NULL REFERENCES batches (id),
    sample_b_id INTEGER NOT NULL REFERENCES batches (id),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- 无序对按 id 升序存储：sample_a_id < sample_b_id 且主键唯一，
    -- 同一对无论登记顺序如何都不会产生第二条
    CHECK (sample_a_id < sample_b_id),
    UNIQUE (sample_a_id, sample_b_id)
);
"""


def database_path() -> str:
    """数据库文件路径，可通过环境变量 DATABASE_PATH 覆盖。"""
    return os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(database_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """开启 IMMEDIATE 写事务：立即获取 RESERVED 锁，使不同连接的写事务串行化。

    禁配规则登记与混样创建若各自“先查后写”，在并发下可能相互漏检
    （一方的新规则在另一方的可见性快照之后生效）。统一在 IMMEDIATE
    事务内完成检查与写入：后到的写事务在 BEGIN 处即被阻塞，直到先到者
    提交，随后读到的必然包含刚提交的规则，从而避免漏检。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def init_db() -> None:
    """建表（幂等），应用启动时调用。"""
    path = Path(database_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
