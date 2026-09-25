"""SQLite 访问层：连接获取与建表初始化。"""
from __future__ import annotations

import os
import sqlite3
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
"""


def database_path() -> str:
    """数据库文件路径，可通过环境变量 DATABASE_PATH 覆盖。"""
    return os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(database_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
