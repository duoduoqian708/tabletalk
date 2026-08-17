"""会话管理存储：AI 对话会话 + 消息（SQLite，data_dir/chat.db）。

语义（产品约定）：
- 只有 AI 对话框提问（POST /ai/chat）才 upsert 会话 / 追加消息，并刷新 updated_at。
- 点"运行"执行 SQL 不进会话——只进审计（audit logger）。
- 查看会话（GET）只读，绝不刷新 updated_at。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  connection_id TEXT NOT NULL,
  title TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  role TEXT NOT NULL,
  kind TEXT NOT NULL,
  content TEXT,
  sql TEXT,
  verdict TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msgs_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_conv_conn ON conversations(connection_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ChatStore:
    def __init__(self, data_dir: Path):
        self.db_path = Path(data_dir) / "chat.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def upsert(self, session_id: str | None, connection_id: str, title: str | None) -> str:
        """提问时调用：无 session_id 则建新会话；有则更新 updated_at（title 传 None 不覆盖）。"""
        now = _now()
        sid = session_id or f"s{uuid.uuid4().hex[:16]}"
        with self._conn() as c:
            row = c.execute("SELECT id FROM conversations WHERE id=?", (sid,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO conversations (id, connection_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                    (sid, connection_id, title, now, now),
                )
            else:
                if title is not None:
                    c.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=?", (title, now, sid))
                else:
                    c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, sid))
        return sid

    def append_messages(self, session_id: str, messages: list[dict]) -> None:
        """流结束后落库（user 消息 + assistant text/sql_card）。"""
        if not messages:
            return
        now = _now()
        with self._conn() as c:
            for m in messages:
                c.execute(
                    "INSERT INTO messages (conversation_id, role, kind, content, sql, verdict, created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        session_id,
                        m.get("role", ""),
                        m.get("kind", "text"),
                        m.get("content"),
                        m.get("sql"),
                        m.get("verdict"),
                        now,
                    ),
                )

    def set_title(self, session_id: str, title: str) -> bool:
        """前端异步生成标题后写回；只改 title 与 updated_at 之外的字段都不动，且**不**刷新 updated_at。"""
        with self._conn() as c:
            cur = c.execute("UPDATE conversations SET title=? WHERE id=?", (title, session_id))
            return cur.rowcount > 0

    def list(self, connection_id: str | None = None, limit: int = 50) -> list[dict]:
        """只读列表：按 updated_at 倒序，附消息数。"""
        q = """
            SELECT c.id, c.connection_id, c.title, c.created_at, c.updated_at,
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count
            FROM conversations c
            {where}
            ORDER BY c.updated_at DESC
            LIMIT ?
        """
        where = "WHERE c.connection_id = ?" if connection_id else ""
        params: tuple = (connection_id, limit) if connection_id else (limit,)
        with self._conn() as c:
            rows = c.execute(q.format(where=where), params).fetchall()
        return [dict(r) for r in rows]

    def get(self, session_id: str) -> dict | None:
        """只读详情：会话 + 全部消息。"""
        with self._conn() as c:
            conv = c.execute("SELECT * FROM conversations WHERE id=?", (session_id,)).fetchone()
            if conv is None:
                return None
            msgs = c.execute(
                "SELECT id, role, kind, content, sql, verdict, created_at FROM messages WHERE conversation_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        return {**dict(conv), "messages": [dict(m) for m in msgs]}
