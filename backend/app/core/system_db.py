"""系统库：统一 SQLite 持久化（tabletalk.db），替代 *.json 本地文件库。

设计：
- audit.db 独立（写多，量大），本库承载 connections/settings/users/approvals/questions/skills/codify（读多）
- 启动时一次性迁移：若表空且旧 *.json/*.log 存在 → 读 JSON → 插表 → 重命名为 *.json.bak（幂等）
- 并发：WAL + threading.Lock + 短事务
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


DB_NAME = "tabletalk.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init_system_db(data_dir: Path) -> Path:
    db_path = Path(data_dir) / DB_NAME
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(db_path)
    try:
        # 连接
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS connections (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        # 系统 KV（settings 存 k='settings' 的 JSON）
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS system_kv (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL
            )
            """
        )
        # 用户
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
        # 审批
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status)")
        # 问答模板
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY,
                conn_id TEXT NOT NULL,
                question TEXT NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_questions_conn ON questions(conn_id)")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_conn_q ON questions(conn_id, question)")
        # 技能
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS skills (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL
            )
            """
        )
        # 代号化映射
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS codify (
                conn_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                code TEXT NOT NULL,
                original TEXT NOT NULL,
                PRIMARY KEY (conn_id, kind, code)
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_codify_conn ON codify(conn_id)")
        con.commit()
    finally:
        con.close()
    return db_path


# 全局锁（进程级）
_lock = threading.Lock()


def get_conn(data_dir: Path) -> sqlite3.Connection:
    db_path = Path(data_dir) / DB_NAME
    if not db_path.exists():
        init_system_db(data_dir)
    return _connect(db_path)
