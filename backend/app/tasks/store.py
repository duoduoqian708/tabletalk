"""任务运行记录：jobs.db（单表 runs）。任务定义在脚本头里，这里不存定义，只存执行史。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from app.core.timeutil import utcnow_iso


class JobStore:
    def __init__(self, data_dir: Path) -> None:
        self.db = Path(data_dir) / "jobs.db"
        self._lock = threading.Lock()
        self._ensure()

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        return con

    def _ensure(self) -> None:
        with self._lock:
            con = self._conn()
            try:
                con.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_name TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        status TEXT,
                        summary TEXT,
                        output TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job_name, id DESC);
                    """
                )
                con.commit()
            finally:
                con.close()

    def start_run(self, job_name: str) -> int:
        now = utcnow_iso()
        with self._lock:
            con = self._conn()
            try:
                cur = con.execute(
                    "INSERT INTO runs (job_name, started_at, status) VALUES (?,?,?)",
                    (job_name, now, "running"),
                )
                con.commit()
                return int(cur.lastrowid)
            finally:
                con.close()

    def finish_run(self, run_id: int, status: str, summary: str | None = None, output: str | None = None) -> None:
        with self._lock:
            con = self._conn()
            try:
                con.execute(
                    "UPDATE runs SET finished_at=?, status=?, summary=?, output=? WHERE id=?",
                    (utcnow_iso(), status, summary or "", output or "", int(run_id)),
                )
                con.commit()
            finally:
                con.close()

    def recent_runs(self, job_name: str, limit: int = 20) -> list[dict]:
        with self._lock:
            con = self._conn()
            con.row_factory = sqlite3.Row
            try:
                rows = con.execute(
                    "SELECT * FROM runs WHERE job_name=? ORDER BY id DESC LIMIT ?",
                    (job_name, int(limit)),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()

    def last(self, job_name: str) -> dict | None:
        runs = self.recent_runs(job_name, 1)
        return runs[0] if runs else None

    def recent_summaries(self, limit: int = 50) -> dict[str, dict]:
        """各任务最近一次运行（供列表 last_run/last_status 快速展示）。"""
        with self._lock:
            con = self._conn()
            con.row_factory = sqlite3.Row
            try:
                rows = con.execute(
                    "SELECT job_name, status, summary, started_at, finished_at FROM runs "
                    "WHERE id IN (SELECT MAX(id) FROM runs GROUP BY job_name) ORDER BY job_name"
                ).fetchall()
                return {dict(r)["job_name"]: dict(r) for r in rows}
            finally:
                con.close()

    def clear(self, job_name: str) -> None:
        with self._lock:
            con = self._conn()
            try:
                con.execute("DELETE FROM runs WHERE job_name=?", (job_name,))
                con.commit()
            finally:
                con.close()