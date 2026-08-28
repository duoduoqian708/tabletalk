"""知识库存储层：每连接独立 SQLite，存文档（含 draft/confirmed 状态）和领域标签。

设计原则：
- 标签只认 confirmed（draft 不参与路由）
- 文档草稿走 draft → confirm 两步，不由 AI 直接写入
- 每个 connection_id 独立一套数据
"""
from __future__ import annotations

import json
import sqlite3
import threading
from app.core.timeutil import utcnow_iso
from pathlib import Path
from typing import Any


class KnowledgeStore:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._lock = threading.Lock()

    def _db_path(self, conn_id: str) -> Path:
        return self._data_dir / f"knowledge-{conn_id}.db"

    def _get_conn(self, conn_id: str) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path(conn_id))
        con.row_factory = sqlite3.Row
        return con

    def _ensure_tables(self, conn_id: str) -> None:
        with self._lock:
            con = self._get_conn(conn_id)
            try:
                con.executescript("""
                    CREATE TABLE IF NOT EXISTS docs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        table_name TEXT NOT NULL,
                        content TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'draft',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS tags (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL DEFAULT 'draft',
                        tables_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_docs_table ON docs(table_name);
                    CREATE INDEX IF NOT EXISTS idx_tags_status ON tags(status);
                """)
                con.commit()
            finally:
                con.close()

    # ---- 文档 CRUD ----

    def create_doc(self, conn_id: str, table_name: str, content: str) -> dict:
        self._ensure_tables(conn_id)
        now = utcnow_iso()
        with self._lock:
            con = self._get_conn(conn_id)
            try:
                cur = con.execute(
                    "INSERT INTO docs (table_name, content, status, created_at, updated_at) VALUES (?,?, 'draft', ?, ?)",
                    (table_name, content, now, now),
                )
                con.commit()
                return {"id": cur.lastrowid, "table_name": table_name, "content": content, "status": "draft", "created_at": now}
            finally:
                con.close()

    def update_doc(self, conn_id: str, doc_id: int, content: str) -> dict | None:
        self._ensure_tables(conn_id)
        now = utcnow_iso()
        with self._lock:
            con = self._get_conn(conn_id)
            try:
                con.execute("UPDATE docs SET content=?, updated_at=? WHERE id=?", (content, now, doc_id))
                con.commit()
                row = con.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
                return dict(row) if row else None
            finally:
                con.close()

    def confirm_doc(self, conn_id: str, doc_id: int) -> dict | None:
        self._ensure_tables(conn_id)
        now = utcnow_iso()
        with self._lock:
            con = self._get_conn(conn_id)
            try:
                con.execute("UPDATE docs SET status='confirmed', updated_at=? WHERE id=?", (now, doc_id))
                con.commit()
                row = con.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
                return dict(row) if row else None
            finally:
                con.close()

    def reject_doc(self, conn_id: str, doc_id: int) -> bool:
        self._ensure_tables(conn_id)
        with self._lock:
            con = self._get_conn(conn_id)
            try:
                con.execute("DELETE FROM docs WHERE id=? AND status='draft'", (doc_id,))
                con.commit()
                return con.total_changes > 0
            finally:
                con.close()

    def list_docs(self, conn_id: str, table_name: str | None = None, status: str | None = None) -> list[dict]:
        self._ensure_tables(conn_id)
        with self._lock:
            con = self._get_conn(conn_id)
            try:
                where, params = [], []
                if table_name:
                    where.append("table_name = ?")
                    params.append(table_name)
                if status:
                    where.append("status = ?")
                    params.append(status)
                sql = "SELECT * FROM docs"
                if where:
                    sql += " WHERE " + " AND ".join(where)
                sql += " ORDER BY id DESC"
                return [dict(r) for r in con.execute(sql, params).fetchall()]
            finally:
                con.close()

    def search_docs(self, conn_id: str, keyword: str) -> list[dict]:
        self._ensure_tables(conn_id)
        with self._lock:
            con = self._get_conn(conn_id)
            try:
                rows = con.execute(
                    "SELECT * FROM docs WHERE content LIKE ? AND status='confirmed' ORDER BY id DESC LIMIT 20",
                    (f"%{keyword}%",),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()

    def get_confirmed_context(self, conn_id: str, table_name: str, max_docs: int = 5) -> str:
        """为指定表返回已确认文档的文本摘要，供上下文注入。"""
        docs = self.list_docs(conn_id, table_name=table_name, status="confirmed")
        if not docs:
            return ""
        parts = [f"[{d['table_name']}] {d['content']}" for d in docs[:max_docs]]
        return "\n".join(parts)

    # ---- 标签 CRUD ----

    def create_tag(self, conn_id: str, name: str, tables: list[str]) -> dict:
        self._ensure_tables(conn_id)
        now = utcnow_iso()
        with self._lock:
            con = self._get_conn(conn_id)
            try:
                con.execute(
                    "INSERT OR IGNORE INTO tags (name, status, tables_json, created_at, updated_at) VALUES (?, 'draft', ?, ?, ?)",
                    (name, json.dumps(tables), now, now),
                )
                con.commit()
                row = con.execute("SELECT * FROM tags WHERE name=?", (name,)).fetchone()
                return dict(row) if row else {"name": name, "status": "draft", "tables_json": json.dumps(tables)}
            finally:
                con.close()

    def confirm_tag(self, conn_id: str, tag_name: str) -> bool:
        self._ensure_tables(conn_id)
        now = utcnow_iso()
        with self._lock:
            con = self._get_conn(conn_id)
            try:
                con.execute("UPDATE tags SET status='confirmed', updated_at=? WHERE name=?", (now, tag_name))
                con.commit()
                return con.total_changes > 0
            finally:
                con.close()

    def reject_tag(self, conn_id: str, tag_name: str) -> bool:
        self._ensure_tables(conn_id)
        with self._lock:
            con = self._get_conn(conn_id)
            try:
                con.execute("DELETE FROM tags WHERE name=? AND status='draft'", (tag_name,))
                con.commit()
                return con.total_changes > 0
            finally:
                con.close()

    def list_tags(self, conn_id: str, status: str | None = None) -> list[dict]:
        self._ensure_tables(conn_id)
        with self._lock:
            con = self._get_conn(conn_id)
            try:
                if status:
                    rows = con.execute("SELECT * FROM tags WHERE status=? ORDER BY name", (status,)).fetchall()
                else:
                    rows = con.execute("SELECT * FROM tags ORDER BY name").fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()

    def get_confirmed_tags(self, conn_id: str) -> list[dict]:
        return self.list_tags(conn_id, status="confirmed")

    def route_tables(self, conn_id: str, tags: list[str]) -> list[str]:
        """根据已确认标签返回关联表集合（标签→表的映射）。"""
        confirmed = self.get_confirmed_tags(conn_id)
        tag_map = {}
        for t in confirmed:
            tag_map[t["name"]] = json.loads(t.get("tables_json", "[]"))
        result = set()
        for tag in tags:
            if tag in tag_map:
                result.update(tag_map[tag])
        return sorted(result)
