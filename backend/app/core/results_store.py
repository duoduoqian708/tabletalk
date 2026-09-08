"""统一报告存储：results.db（data_dir）。

两类来源的"报告结果"统一落这里，正文以文本存储（md/html），文件系统副本仅作
调试/导出（任务侧 lib.py report() 仍写文件，runner 收编进库）：
- source='job'  ：定时任务运行产出（runner 收编 reports/*.md）
- source='chat' ：AI 对话报告模式产出（report_stream narration 完成）

键设计：
- id          行主键（uuid），多份并存互不覆盖
- group_id    分组过滤：chat=session_id / job=job_name
- source_id   每份唯一：chat=<session_id>#<report_id> / job=<job_name>#<run_id>
- meta        JSON 细粒度溯源（report_id 与审计系统对齐，可回溯每章查询）
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path

from app.core.timeutil import utcnow_iso

MAX_CONTENT_BYTES = 2 * 1024 * 1024  # 正文防御上限 2MB


class ResultsStore:
    def __init__(self, data_dir: Path) -> None:
        self.db = Path(data_dir) / "results.db"
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
                    CREATE TABLE IF NOT EXISTS results (
                        id TEXT PRIMARY KEY,
                        source TEXT NOT NULL,
                        group_id TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        connection_id TEXT,
                        title TEXT,
                        format TEXT NOT NULL DEFAULT 'md',
                        content TEXT NOT NULL,
                        size INTEGER,
                        meta TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_results_created ON results(created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_results_group ON results(source, group_id);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_results_source_id ON results(source_id);
                    """
                )
                con.commit()
            finally:
                con.close()

    def save_result(self, source: str, group_id: str, source_id: str, content: str,
                    title: str = "", connection_id: str = "", fmt: str = "md",
                    meta: dict | None = None) -> str | None:
        """保存一份报告。source_id 已存在则更新（同一次生成重放）；超限拒绝返回 None。"""
        if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            return None
        rid = uuid.uuid4().hex[:20]
        with self._lock:
            con = self._conn()
            try:
                con.execute(
                    "INSERT INTO results (id, source, group_id, source_id, connection_id, title,"
                    " format, content, size, meta, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(source_id) DO UPDATE SET"
                    " title=excluded.title, content=excluded.content, size=excluded.size,"
                    " meta=excluded.meta, created_at=excluded.created_at",
                    (rid, source, group_id, source_id, connection_id or "", title or "",
                     fmt or "md", content, len(content.encode("utf-8")),
                     json.dumps(meta or {}, ensure_ascii=False), utcnow_iso()),
                )
                con.commit()
                return rid
            finally:
                con.close()

    def list_results(self, source: str | None = None, group_id: str | None = None,
                     limit: int = 50) -> list[dict]:
        """列表（不含正文），created_at 倒序。"""
        q = ("SELECT id, source, group_id, source_id, connection_id, title, format, size, meta, created_at"
             " FROM results {where} ORDER BY created_at DESC, rowid DESC LIMIT ?")
        conds, params = [], []
        if source:
            conds.append("source=?")
            params.append(source)
        if group_id:
            conds.append("group_id=?")
            params.append(group_id)
        params.append(int(limit))
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        with self._lock:
            con = self._conn()
            try:
                rows = con.execute(q.format(where=where), params).fetchall()
            finally:
                con.close()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["meta"] = json.loads(d.get("meta") or "{}")
            except (ValueError, TypeError):
                d["meta"] = {}
            out.append(d)
        return out

    def get_result(self, rid: str) -> dict | None:
        with self._lock:
            con = self._conn()
            try:
                row = con.execute("SELECT * FROM results WHERE id=?", (rid,)).fetchone()
            finally:
                con.close()
        if row is None:
            return None
        d = dict(row)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except (ValueError, TypeError):
            d["meta"] = {}
        return d

    def cleanup(self, keep_n: int = 500) -> int:
        """按总数保留最新 keep_n 条，返回删除条数（供内置清理任务调用）。"""
        keep_n = max(0, int(keep_n))
        with self._lock:
            con = self._conn()
            try:
                cur = con.execute(
                    "DELETE FROM results WHERE id NOT IN"
                    " (SELECT id FROM results ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                    (keep_n,),
                )
                con.commit()
                return cur.rowcount
            finally:
                con.close()

    def count(self) -> int:
        with self._lock:
            con = self._conn()
            try:
                return int(con.execute("SELECT COUNT(*) AS n FROM results").fetchone()["n"])
            finally:
                con.close()
