"""成本追踪：记录每次 AI 调用的 token 用量、模型、耗时，存入 SQLite。"""
from __future__ import annotations

import sqlite3
import threading
from app.core.timeutil import utcnow_iso, utcnow_minus_days
from pathlib import Path
from typing import Any


class CostTracker:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._lock = threading.Lock()
        self._ensure_tables()

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._data_dir / "cost.db")
        con.row_factory = sqlite3.Row
        return con

    def _ensure_tables(self) -> None:
        with self._lock:
            con = self._conn()
            try:
                con.executescript("""
                    CREATE TABLE IF NOT EXISTS cost_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        connection TEXT,
                        skill TEXT,
                        model TEXT,
                        provider TEXT,
                        input_tokens INTEGER DEFAULT 0,
                        output_tokens INTEGER DEFAULT 0,
                        total_tokens INTEGER DEFAULT 0,
                        elapsed_ms REAL,
                        estimated_cost_usd REAL DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS idx_cost_ts ON cost_log(ts);
                    CREATE INDEX IF NOT EXISTS idx_cost_skill ON cost_log(skill);
                """)
                con.commit()
            finally:
                con.close()

    def log(self, connection: str | None = None, skill: str | None = None,
            model: str | None = None, provider: str | None = None,
            input_tokens: int = 0, output_tokens: int = 0,
            elapsed_ms: float | None = None) -> None:
        now = utcnow_iso()
        total = input_tokens + output_tokens
        # 粗略成本估算（美元）：按 GPT-4o-mini 定价估算，可后续精确化
        cost = (input_tokens * 0.15 + output_tokens * 0.6) / 1_000_000
        with self._lock:
            con = self._conn()
            try:
                con.execute(
                    "INSERT INTO cost_log (ts, connection, skill, model, provider, input_tokens, output_tokens, total_tokens, elapsed_ms, estimated_cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (now, connection, skill, model, provider, input_tokens, output_tokens, total, elapsed_ms, cost),
                )
                con.commit()
            finally:
                con.close()

    def get_summary(self, from_ts: str | None = None, to_ts: str | None = None) -> dict:
        where, params = [], []
        if from_ts:
            where.append("ts >= ?")
            params.append(from_ts)
        if to_ts:
            where.append("ts <= ?")
            params.append(to_ts)
        where_clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            con = self._conn()
            try:
                row = con.execute(
                    f"SELECT COUNT(*) as count, SUM(input_tokens) as input_tokens, SUM(output_tokens) as output_tokens, SUM(total_tokens) as total_tokens, SUM(estimated_cost_usd) as total_cost FROM cost_log{where_clause}",
                    params,
                ).fetchone()
                by_skill = con.execute(
                    f"SELECT skill, COUNT(*) as count, SUM(total_tokens) as tokens, SUM(estimated_cost_usd) as cost FROM cost_log{where_clause} GROUP BY skill ORDER BY tokens DESC",
                    params,
                ).fetchall()
                return {
                    "total_calls": row["count"] or 0,
                    "input_tokens": row["input_tokens"] or 0,
                    "output_tokens": row["output_tokens"] or 0,
                    "total_tokens": row["total_tokens"] or 0,
                    "total_cost_usd": round(row["total_cost"] or 0, 6),
                    "by_skill": [dict(r) for r in by_skill],
                }
            finally:
                con.close()

    def cleanup(self, keep_days: int = 30) -> int:
        """删除 keep_days 天前的记录，返回删除行数。"""
        cutoff = utcnow_minus_days(keep_days)
        with self._lock:
            con = self._conn()
            try:
                cur = con.execute("DELETE FROM cost_log WHERE ts < ?", (cutoff,))
                con.commit()
                return cur.rowcount
            finally:
                con.close()

    def get_daily(self, from_ts: str | None = None, to_ts: str | None = None) -> list[dict]:
        where, params = [], []
        if from_ts:
            where.append("ts >= ?")
            params.append(from_ts)
        if to_ts:
            where.append("ts <= ?")
            params.append(to_ts)
        where_clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            con = self._conn()
            try:
                rows = con.execute(
                    f"SELECT substr(ts, 1, 10) as date, COUNT(*) as calls, SUM(total_tokens) as tokens, SUM(estimated_cost_usd) as cost FROM cost_log{where_clause} GROUP BY date ORDER BY date DESC LIMIT 90",
                    params,
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()
