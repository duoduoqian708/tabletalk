"""任务持久化：SQLite（system_db），存任务定义与执行历史。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class TaskStore:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._lock = threading.Lock()
        self._ensure_tables()

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._data_dir / "tasks.db")
        con.row_factory = sqlite3.Row
        return con

    def _ensure_tables(self) -> None:
        with self._lock:
            con = self._conn()
            try:
                con.executescript("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        cron TEXT NOT NULL,
                        sql TEXT,
                        natural_query TEXT,
                        connection_id TEXT NOT NULL,
                        skill TEXT DEFAULT 'query',
                        enabled INTEGER DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_run_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS task_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        status TEXT DEFAULT 'running',
                        result_summary TEXT,
                        FOREIGN KEY (task_id) REFERENCES tasks(id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_runs_task ON task_runs(task_id);
                """)
                con.commit()
            finally:
                con.close()

    def create(self, name: str, cron: str, connection_id: str, sql: str | None = None,
               natural_query: str | None = None, skill: str = "query") -> dict:
        task_id = f"task_{uuid.uuid4().hex[:10]}"
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            con = self._conn()
            try:
                con.execute(
                    "INSERT INTO tasks (id, name, cron, sql, natural_query, connection_id, skill, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (task_id, name, cron, sql, natural_query, connection_id, skill, now, now),
                )
                con.commit()
                return self.get(task_id)
            finally:
                con.close()

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            con = self._conn()
            try:
                row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                return dict(row) if row else None
            finally:
                con.close()

    def list_all(self, enabled_only: bool = False) -> list[dict]:
        with self._lock:
            con = self._conn()
            try:
                if enabled_only:
                    rows = con.execute("SELECT * FROM tasks WHERE enabled=1 ORDER BY created_at DESC").fetchall()
                else:
                    rows = con.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()

    def update(self, task_id: str, **kwargs) -> dict | None:
        allowed = {"name", "cron", "sql", "natural_query", "connection_id", "enabled"}
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        if not updates:
            return self.get(task_id)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        updates["updated_at"] = now
        set_clause = ", ".join(f"{k}=?" for k in updates)
        params = list(updates.values()) + [task_id]
        with self._lock:
            con = self._conn()
            try:
                con.execute(f"UPDATE tasks SET {set_clause} WHERE id=?", params)
                con.commit()
                return self.get(task_id)
            finally:
                con.close()

    def delete(self, task_id: str) -> bool:
        with self._lock:
            con = self._conn()
            try:
                con.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                con.execute("DELETE FROM task_runs WHERE task_id=?", (task_id,))
                con.commit()
                return con.total_changes > 0
            finally:
                con.close()

    def log_run(self, task_id: str) -> int:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            con = self._conn()
            try:
                cur = con.execute(
                    "INSERT INTO task_runs (task_id, started_at) VALUES (?,?)",
                    (task_id, now),
                )
                con.execute("UPDATE tasks SET last_run_at=?, updated_at=? WHERE id=?", (now, now, task_id))
                con.commit()
                return cur.lastrowid
            finally:
                con.close()

    def finish_run(self, run_id: int, status: str, summary: str | None = None) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            con = self._conn()
            try:
                con.execute(
                    "UPDATE task_runs SET finished_at=?, status=?, result_summary=? WHERE id=?",
                    (now, status, summary, run_id),
                )
                con.commit()
            finally:
                con.close()

    def get_runs(self, task_id: str, limit: int = 10) -> list[dict]:
        with self._lock:
            con = self._conn()
            try:
                rows = con.execute(
                    "SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT ?",
                    (task_id, limit),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()
