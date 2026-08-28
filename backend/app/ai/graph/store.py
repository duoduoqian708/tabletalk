"""图谱存储层：表间关联边（FK + 值重叠），每连接独立 SQLite。"""
from __future__ import annotations

import json
import sqlite3
import threading
from app.core.timeutil import utcnow_iso
from pathlib import Path
from typing import Any


class GraphStore:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._lock = threading.Lock()

    def _db_path(self, conn_id: str) -> Path:
        return self._data_dir / f"graph-{conn_id}.db"

    def _ensure_tables(self, conn_id: str) -> None:
        with self._lock:
            con = sqlite3.connect(self._db_path(conn_id))
            try:
                con.executescript("""
                    CREATE TABLE IF NOT EXISTS edges (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_table TEXT NOT NULL,
                        target_table TEXT NOT NULL,
                        relation TEXT NOT NULL DEFAULT 'related',
                        source_col TEXT,
                        target_col TEXT,
                        metadata TEXT DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_table);
                    CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_table);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_pair ON edges(source_table, target_table, relation);
                """)
                con.commit()
            finally:
                con.close()

    def add_edge(self, conn_id: str, source: str, target: str, relation: str = "related",
                 source_col: str | None = None, target_col: str | None = None) -> dict:
        self._ensure_tables(conn_id)
        now = utcnow_iso()
        with self._lock:
            con = sqlite3.connect(self._db_path(conn_id))
            try:
                con.execute(
                    "INSERT OR REPLACE INTO edges (source_table, target_table, relation, source_col, target_col, metadata, created_at) VALUES (?,?,?,?,?,?,?)",
                    (source, target, relation, source_col, target_col, "{}", now),
                )
                con.commit()
                return {"source": source, "target": target, "relation": relation}
            finally:
                con.close()

    def remove_edge(self, conn_id: str, source: str, target: str, relation: str | None = None) -> bool:
        self._ensure_tables(conn_id)
        with self._lock:
            con = sqlite3.connect(self._db_path(conn_id))
            try:
                if relation:
                    con.execute("DELETE FROM edges WHERE source_table=? AND target_table=? AND relation=?", (source, target, relation))
                else:
                    con.execute("DELETE FROM edges WHERE source_table=? AND target_table=?", (source, target))
                con.commit()
                return con.total_changes > 0
            finally:
                con.close()

    def get_neighbors(self, conn_id: str, table_name: str, hops: int = 2) -> list[dict]:
        """获取表的关联表（N 跳 BFS）。"""
        self._ensure_tables(conn_id)
        with self._lock:
            con = sqlite3.connect(self._db_path(conn_id))
            con.row_factory = sqlite3.Row
            try:
                visited = set()
                result = []
                queue = [(table_name, 0)]
                while queue:
                    current, depth = queue.pop(0)
                    if current in visited or depth > hops:
                        continue
                    visited.add(current)
                    rows = con.execute(
                        "SELECT * FROM edges WHERE source_table=? OR target_table=?",
                        (current, current),
                    ).fetchall()
                    for r in rows:
                        neighbor = r["target_table"] if r["source_table"] == current else r["source_table"]
                        if neighbor not in visited:
                            result.append({
                                "source": r["source_table"],
                                "target": r["target_table"],
                                "relation": r["relation"],
                                "source_col": r["source_col"],
                                "target_col": r["target_col"],
                            })
                            if depth < hops:
                                queue.append((neighbor, depth + 1))
                return result
            finally:
                con.close()

    def list_edges(self, conn_id: str, table_name: str | None = None) -> list[dict]:
        self._ensure_tables(conn_id)
        with self._lock:
            con = sqlite3.connect(self._db_path(conn_id))
            con.row_factory = sqlite3.Row
            try:
                if table_name:
                    rows = con.execute(
                        "SELECT * FROM edges WHERE source_table=? OR target_table=? ORDER BY id",
                        (table_name, table_name),
                    ).fetchall()
                else:
                    rows = con.execute("SELECT * FROM edges ORDER BY id LIMIT 200").fetchall()
                return [dict(r) for r in rows]
            finally:
                con.close()

    def auto_build_from_schema(self, conn_id: str, schema: dict) -> int:
        """从 schema FK 自动建边。返回新增边数。"""
        count = 0
        for table_name, table_info in (schema or {}).items():
            fks = table_info.get("foreign_keys", []) if isinstance(table_info, dict) else []
            for fk in fks:
                ref_table = fk.get("ref_table") or fk.get("referred_table", "")
                if ref_table:
                    self.add_edge(
                        conn_id, table_name, ref_table, relation="fk",
                        source_col=fk.get("column"), target_col=fk.get("ref_column"),
                    )
                    count += 1
        return count
