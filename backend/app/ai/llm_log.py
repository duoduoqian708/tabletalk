"""LLM 调用日志：每次模型调用的入参/返回/用量存 SQLite，按会话聚合，定期清理。"""
from __future__ import annotations

import json
import sqlite3
import threading
from app.core.timeutil import utcnow_iso, utcnow_minus_days
from pathlib import Path
from typing import Any

_MAX_JSON_BYTES = 100 * 1024  # 100KB 截断，防超大上下文撑爆磁盘


def _truncate_json(obj: Any, max_bytes: int = _MAX_JSON_BYTES) -> str | None:
    """将对象序列化为 JSON 字符串，超过 max_bytes 截断。"""
    if obj is None:
        return None
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return None
    if len(s.encode("utf-8")) > max_bytes:
        truncated = s[:max_bytes // 2]
        return truncated + f"...(truncated, total {len(s)} chars)"
    return s


class LlmCallLog:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._lock = threading.Lock()
        self._ensure_tables()

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._data_dir / "llm_calls.db")
        con.row_factory = sqlite3.Row
        return con

    def _ensure_tables(self) -> None:
        with self._lock:
            con = self._conn()
            try:
                con.executescript("""
                    CREATE TABLE IF NOT EXISTS llm_call_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        conn_id TEXT,
                        skill TEXT,
                        model TEXT,
                        provider TEXT,
                        session_id TEXT,
                        request_json TEXT,
                        response_json TEXT,
                        input_tokens INTEGER DEFAULT 0,
                        output_tokens INTEGER DEFAULT 0,
                        elapsed_ms REAL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_llm_ts ON llm_call_log(ts);
                    CREATE INDEX IF NOT EXISTS idx_llm_conn ON llm_call_log(conn_id);
                    CREATE INDEX IF NOT EXISTS idx_llm_session ON llm_call_log(session_id);
                """)
                # 容错：给已有表加 session_id 列（ALTER TABLE 不存在列会报错，忽略即可）
                try:
                    con.execute("ALTER TABLE llm_call_log ADD COLUMN session_id TEXT")
                except Exception:
                    pass
                try:
                    con.execute("CREATE INDEX IF NOT EXISTS idx_llm_session ON llm_call_log(session_id)")
                except Exception:
                    pass
                con.commit()
            finally:
                con.close()

    def log(
        self,
        *,
        ts: str | None = None,
        conn_id: str | None = None,
        skill: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        session_id: str | None = None,
        request_json: Any = None,
        response_json: Any = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        elapsed_ms: float | None = None,
    ) -> None:
        now = ts or utcnow_iso()
        req_str = _truncate_json(request_json)
        resp_str = _truncate_json(response_json)
        with self._lock:
            con = self._conn()
            try:
                con.execute(
                    "INSERT INTO llm_call_log (ts, conn_id, skill, model, provider, session_id, request_json, response_json, input_tokens, output_tokens, elapsed_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now, conn_id, skill, model, provider, session_id, req_str, resp_str, input_tokens, output_tokens, elapsed_ms, now),
                )
                con.commit()
            finally:
                con.close()

    def cleanup(self, keep_days: int = 30) -> int:
        cutoff = utcnow_minus_days(keep_days)
        with self._lock:
            con = self._conn()
            try:
                cur = con.execute("DELETE FROM llm_call_log WHERE ts < ?", (cutoff,))
                con.commit()
                return cur.rowcount
            finally:
                con.close()

    def daily_agg(self, from_ts: str | None = None, to_ts: str | None = None) -> list[dict]:
        """按日聚合：返回 [{date, calls, sessions, input_tokens, output_tokens, total_tokens, elapsed_ms_avg}]"""
        where, params = self._where(from_ts, to_ts)
        with self._lock:
            con = self._conn()
            try:
                rows = con.execute(
                    f"""SELECT substr(ts, 1, 10) as date,
                               COUNT(*) as calls,
                               COUNT(DISTINCT session_id) as sessions,
                               SUM(input_tokens) as input_tokens,
                               SUM(output_tokens) as output_tokens,
                               SUM(input_tokens + output_tokens) as total_tokens,
                               ROUND(AVG(elapsed_ms), 0) as elapsed_ms_avg
                        FROM llm_call_log{where}
                        GROUP BY date ORDER BY date""",
                    params,
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()

    def sessions_agg(self, from_ts: str | None = None, to_ts: str | None = None) -> list[dict]:
        """按 session 聚合：返回 [{session_id, calls, total_tokens, first_ts, last_ts, skills, conn_id}]"""
        where, params = self._where(from_ts, to_ts)
        with self._lock:
            con = self._conn()
            try:
                rows = con.execute(
                    f"""SELECT COALESCE(session_id, '__none__') AS session_id,
                               COUNT(*) as calls,
                               SUM(input_tokens + output_tokens) as total_tokens,
                               SUM(input_tokens) as input_tokens,
                               SUM(OUTPUT_tokens) as output_tokens,
                               MIN(ts) as first_ts,
                               MAX(ts) as last_ts,
                               GROUP_CONCAT(DISTINCT skill) as skills,
                               conn_id
                        FROM llm_call_log{where}
                        GROUP BY session_id
                        ORDER BY MAX(ts) DESC""",
                    params,
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()

    def session_calls(self, session_id: str, from_ts: str | None = None, to_ts: str | None = None) -> list[dict]:
        """某 session 的所有 LLM 调用明细；'__none__' 代指无会话的系统调用（KB 构建/嵌入）。"""
        if session_id == "__none__":
            where_parts = ["session_id IS NULL"]
            params: list = []
        else:
            where_parts = ["session_id = ?"]
            params = [session_id]
        if from_ts:
            where_parts.append("ts >= ?")
            params.append(from_ts)
        if to_ts:
            where_parts.append("ts <= ?")
            params.append(to_ts)
        where = " WHERE " + " AND ".join(where_parts)
        with self._lock:
            con = self._conn()
            try:
                rows = con.execute(
                    f"""SELECT id, ts, conn_id, skill, model, provider, session_id,
                               input_tokens, output_tokens, elapsed_ms
                        FROM llm_call_log{where}
                        ORDER BY id""",
                    params,
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()

    def call_detail(self, call_id: int) -> dict | None:
        """单条调用完整详情（含 request_json / response_json）。"""
        with self._lock:
            con = self._conn()
            try:
                row = con.execute(
                    "SELECT * FROM llm_call_log WHERE id = ?", (call_id,)
                ).fetchone()
                if not row:
                    return None
                d = dict(row)
                # 解析 JSON 字符串为对象方便前端展示
                for key in ("request_json", "response_json"):
                    raw = d.get(key)
                    if raw:
                        try:
                            d[key] = json.loads(raw)
                        except Exception:
                            pass
                return d
            finally:
                con.close()

    def count(self, *, conn_id: str | None = None) -> int:
        where, params = "", []
        if conn_id:
            where = " WHERE conn_id = ?"
            params = [conn_id]
        with self._lock:
            con = self._conn()
            try:
                row = con.execute(f"SELECT COUNT(*) as n FROM llm_call_log{where}", params).fetchone()
                return row["n"] if row else 0
            finally:
                con.close()

    @staticmethod
    def _where(from_ts: str | None, to_ts: str | None) -> tuple[str, list]:
        parts: list[str] = []
        params: list = []
        if from_ts:
            parts.append("ts >= ?")
            params.append(from_ts)
        if to_ts:
            parts.append("ts <= ?")
            params.append(to_ts)
        return (" WHERE " + " AND ".join(parts)) if parts else "", params
