"""知识库存储后端：JsonStorage（回退）/ SqliteStorage（默认，标准格式）。

数据格式标准化：SQLite 文件（knowledge-{conn}.db）含 docs / edges / tags / table_tags /
embeddings / table_embeddings / meta 表——任何 SQLite 工具可读、可审计、可导出；
k-hop 提供标准递归 CTE 查询，向量提供 vec0 虚拟表查询（sqlite-vec 可用时）。

企业版升级路径（pgvector / Qdrant / Neo4j）：实现同一 KbStorage 协议即可，上层零改动。
运行时检索仍走内存（毫秒级），SQLite 是标准持久化格式 + 标准查询接口。
"""
from __future__ import annotations

import json
import logging
import os
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

try:
    import sqlite_vec  # type: ignore

    _VEC_AVAILABLE = True
except Exception:  # pragma: no cover - 依赖缺失回退 JSON
    _VEC_AVAILABLE = False


def _f32_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *[float(v) for v in vec])


def _f32_list(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


KB_SNAPSHOT_VERSION = 2


@dataclass
class KbSnapshot:
    """一个连接的知识库全量快照（与存储格式无关的中间表示）。

    v2：知识按表组织（tables: name -> TableKnowledge.asdict()，含逐列
    ColumnInfo）。v1 工件（列碎片 doc + _enums 字典）作废不迁移。
    """
    version: int = KB_SNAPSHOT_VERSION
    tables: dict[str, Any] = field(default_factory=dict)   # name -> TableKnowledge dict
    auto: list[dict] = field(default_factory=list)   # 结构文档（Task 2 向量化重做前的桥接）
    user: list[dict] = field(default_factory=list)   # 用户手写笔记（usr-，跨版本保留）
    samples: dict[str, Any] = field(default_factory=dict)
    edges: list[dict] = field(default_factory=list)
    vec: dict[str, list[float]] = field(default_factory=dict)
    table_vec: dict[str, list[float]] = field(default_factory=dict)
    tags: dict[str, Any] = field(default_factory=dict)
    table_tags: dict[str, list[str]] = field(default_factory=dict)
    schema: dict[str, Any] = field(default_factory=dict)
    emb_fingerprint: str = ""
    schema_fingerprint: str = ""          # 结构指纹（增量对比用）
    edge_tombstones: list[dict] = field(default_factory=list)  # 用户删除的 overlap 边（不复活）
    excluded: list[str] = field(default_factory=list)          # 图谱视图中移出的表（不影响审查页）
    synced_at: str = ""                   # 最近一次增量同步时间
    llm_graph_edges: list[dict] = field(default_factory=list)  # LLM 发现的 draft 边（待人工确认）
    llm_edge_tombstones: list[dict] = field(default_factory=list)  # 用户拒绝过的 LLM 边（重建不复活）


class KbStorage(Protocol):
    """存储协议：换实现（JSON/SQLite/pgvector/Neo4j）不影响上层。"""
    kind: str

    def exists(self) -> bool: ...

    def load(self) -> KbSnapshot: ...

    def save(self, snap: KbSnapshot) -> None: ...

    # 查询表达力：标准查询接口（企业版/审计/服务化直接复用）
    def hop_sql(self, table: str, hops: int) -> str: ...

    def vec_topn_sql(self, k: int) -> str: ...


# ---------------------------------------------------------------- JSON 回退

class JsonStorage:
    """现有 JSON 格式（knowledge.json + knowledge-{conn}.json），作为回退实现。"""

    kind = "json"

    def __init__(self, data_dir: Path, conn_id: str) -> None:
        self._user_path = data_dir / "knowledge.json"
        self._artifact_path = data_dir / f"knowledge-{conn_id}.json"
        self._conn_id = conn_id

    def exists(self) -> bool:
        return self._artifact_path.exists() or self._user_path.exists()

    def load(self) -> KbSnapshot:
        snap = KbSnapshot()
        # 用户手写标注（跨连接共享文件，格式跨版本稳定 → 旧工件作废也保留）
        if self._user_path.exists():
            try:
                data = json.loads(self._user_path.read_text(encoding="utf-8"))
                snap.user = data.get(self._conn_id, [])
            except Exception as e:
                logger.warning("[kb.storage] %s 用户标注读取失败：%s", self._user_path.name, e)
        # 连接 artifact
        if self._artifact_path.exists():
            try:
                data = json.loads(self._artifact_path.read_text(encoding="utf-8"))
                if data.get("version") != KB_SNAPSHOT_VERSION:
                    logger.warning(
                        "[kb.storage] conn=%s 旧工件作废，请重新构建", self._conn_id,
                    )
                    return snap
                snap.tables = data.get("tables", {})
                snap.auto = data.get("auto", [])
                snap.samples = data.get("samples", {})
                edges = data.get("graph", {}).get("edges", [])
                for e in edges:
                    e.setdefault("cardinality", "n:1")  # 旧库边无基数 → 默认 n:1
                    e.setdefault("reason", "")
                snap.edges = edges
                snap.vec = data.get("vec", {})
                snap.table_vec = data.get("table_vec", {})
                snap.tags = {
                    n: {
                        "description": v.get("description", ""),
                        "status": v.get("status", "draft"),
                        "color": v.get("color", ""),
                    }
                    for n, v in (data.get("tags", {}) or {}).items()
                }
                snap.table_tags = data.get("table_tags", {})
                snap.schema = data.get("schema", {})
                snap.emb_fingerprint = data.get("emb_fingerprint", "")
                snap.schema_fingerprint = data.get("schema_fingerprint", "")
                snap.edge_tombstones = data.get("edge_tombstones", [])
                snap.excluded = data.get("excluded", [])
                snap.synced_at = data.get("synced_at", "")
                snap.llm_graph_edges = data.get("llm_graph_edges", [])
                snap.llm_edge_tombstones = data.get("llm_edge_tombstones", [])
            except Exception as e:
                logger.warning("[kb.storage] %s artifact 读取失败（按空库处理）：%s", self._artifact_path.name, e)
        return snap

    def save(self, snap: KbSnapshot) -> None:
        # 用户标注写共享文件（保持旧格式兼容）
        if snap.user:
            try:
                data = {}
                if self._user_path.exists():
                    data = json.loads(self._user_path.read_text(encoding="utf-8"))
                data[self._conn_id] = snap.user
                self._user_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                logger.warning("[kb.storage] %s 用户标注写盘失败：%s", self._user_path.name, e)
        self._artifact_path.write_text(
            json.dumps({
                "version": KB_SNAPSHOT_VERSION,
                "tables": snap.tables,
                "auto": snap.auto,
                "samples": snap.samples,
                "graph": {"edges": snap.edges},
                "vec": snap.vec,
                "table_vec": snap.table_vec,
                "tags": snap.tags,
                "table_tags": snap.table_tags,
                "schema": snap.schema,
                "emb_fingerprint": snap.emb_fingerprint,
                "schema_fingerprint": snap.schema_fingerprint,
                "edge_tombstones": snap.edge_tombstones,
                "excluded": snap.excluded,
                "synced_at": snap.synced_at,
                "llm_graph_edges": snap.llm_graph_edges,
                "llm_edge_tombstones": snap.llm_edge_tombstones,
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    def hop_sql(self, table: str, hops: int) -> str:
        """JSON 后端没有 SQL——返回等价说明（内存 BFS 由上层提供）。"""
        raise NotImplementedError("JSON 存储不提供 SQL 查询接口，请切换 SqliteStorage")

    def vec_topn_sql(self, k: int) -> str:
        raise NotImplementedError("JSON 存储不提供 SQL 查询接口，请切换 SqliteStorage")


# ---------------------------------------------------------------- SQLite 标准格式

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS docs (
  id TEXT PRIMARY KEY, kind TEXT, title TEXT, body TEXT,
  table_name TEXT, column_name TEXT, status TEXT, source TEXT, tags TEXT,
  conn_id TEXT, updated_at TEXT, archived INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS edges (
  from_table TEXT, from_col TEXT, to_table TEXT, to_col TEXT,
  kind TEXT, weight REAL, shared INTEGER,
  cardinality TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS tags (name TEXT PRIMARY KEY, description TEXT, status TEXT, color TEXT);
CREATE TABLE IF NOT EXISTS table_tags (table_name TEXT PRIMARY KEY, tags TEXT);
CREATE TABLE IF NOT EXISTS embeddings (doc_id TEXT PRIMARY KEY, vec BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS table_embeddings (table_name TEXT PRIMARY KEY, vec BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS version_archive (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_ts TEXT NOT NULL,
  version INTEGER NOT NULL,
  kind TEXT NOT NULL,            -- 'column' | 'table'
  table_name TEXT NOT NULL,
  column_name TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL          -- JSON：{comment, values, example, status, ddl, vector_override}
);
CREATE INDEX IF NOT EXISTS idx_va_lookup ON version_archive(table_name, column_name, batch_ts);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_table);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_table);
"""


class SqliteStorage:
    """标准 SQLite 格式：一个连接一个 .db 文件。首次使用时自动从旧 JSON artifact 迁移。"""

    kind = "sqlite"

    def __init__(self, data_dir: Path, conn_id: str) -> None:
        self._path = data_dir / f"knowledge-{conn_id}.db"
        self._user_json = data_dir / "knowledge.json"
        self._conn_id = conn_id
        self._lock = threading.Lock()
        self._vec_ok = self._probe_vec()

    @staticmethod
    def _probe_vec() -> bool:
        if not _VEC_AVAILABLE:
            return False
        try:
            import sqlite3
            conn = sqlite3.connect(":memory:")
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.execute("select vec_version()").fetchone()
            conn.close()
            return True
        except Exception:
            return False

    def vec_available(self) -> bool:
        return self._vec_ok

    def exists(self) -> bool:
        return self._path.exists()

    # ---- 连接管理 ----
    def _conn(self) -> Any:
        import sqlite3
        conn = sqlite3.connect(str(self._path))
        conn.enable_load_extension(True)
        if self._vec_ok:
            sqlite_vec.load(conn)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self, conn: Any) -> None:
        conn.executescript(_SCHEMA)
        # 旧库迁移：docs 表补 archived 列（已存在则跳过）
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(docs)")}
        if "archived" not in cols:
            conn.execute("ALTER TABLE docs ADD COLUMN archived INTEGER DEFAULT 0")
            conn.commit()
        # 旧库迁移：edges 表补 cardinality/reason 列（边 v2，已存在则跳过）
        ecols = {row["name"] for row in conn.execute("PRAGMA table_info(edges)")}
        for col in ("cardinality", "reason"):
            if col not in ecols:
                conn.execute(f"ALTER TABLE edges ADD COLUMN {col} TEXT")
        if "cardinality" not in ecols or "reason" not in ecols:
            conn.commit()
        # 旧库迁移：tags 表补 color 列（标签颜色后端持久化，已存在则跳过）
        tcols = {row["name"] for row in conn.execute("PRAGMA table_info(tags)")}
        if "color" not in tcols:
            conn.execute("ALTER TABLE tags ADD COLUMN color TEXT")
            conn.commit()
        if self._vec_ok:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS doc_vec USING vec0(doc_id TEXT PRIMARY KEY, vec float[256])")
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS table_vec USING vec0(table_name TEXT PRIMARY KEY, vec float[256])")

    # ---- 读取 ----
    def load(self) -> KbSnapshot:
        return self._read()

    def _read(self) -> KbSnapshot:
        snap = KbSnapshot()
        if not self._path.exists():
            return snap
        try:
            conn = self._conn()
            try:
                # 结构迁移先行：旧库缺列（如 tags.color）在此补齐，避免 SELECT 失败丢数据
                self._init(conn)
                # 版本门控：v1 工件（无 version 或 version<2）作废不迁移，按空库处理
                if self._meta(conn, "version") != str(KB_SNAPSHOT_VERSION):
                    logger.warning(
                        "[kb.storage] conn=%s 旧工件作废，请重新构建", self._conn_id,
                    )
                    return snap
                snap.emb_fingerprint = self._meta(conn, "emb_fingerprint")
                snap.schema_fingerprint = self._meta(conn, "schema_fingerprint")
                snap.synced_at = self._meta(conn, "synced_at")
                tombs = self._meta(conn, "edge_tombstones")
                if tombs:
                    snap.edge_tombstones = json.loads(tombs)
                excl = self._meta(conn, "excluded_tables")
                if excl:
                    snap.excluded = json.loads(excl)
                for row in conn.execute("SELECT * FROM docs"):
                    d = dict(row)
                    d["table"] = d.pop("table_name")
                    d["column"] = d.pop("column_name")
                    d["tags"] = json.loads(d.get("tags") or "[]")
                    d.setdefault("conn_id", self._conn_id)
                    d.setdefault("updated_at", "")
                    d["archived"] = bool(d.get("archived"))
                    (snap.auto if d["source"] != "user" else snap.user).append(d)
                for row in conn.execute("SELECT * FROM edges"):
                    e = dict(row)
                    e["from"] = e.pop("from_table")
                    e["to"] = e.pop("to_table")
                    e.setdefault("cardinality", "n:1")  # 旧库边无基数 → 默认 n:1
                    e.setdefault("reason", "")
                    snap.edges.append(e)
                for row in conn.execute("SELECT name, description, status, color FROM tags"):
                    snap.tags[row["name"]] = {
                        "description": row["description"] or "",
                        "status": row["status"],
                        "color": row["color"] or "",
                    }
                for row in conn.execute("SELECT table_name, tags FROM table_tags"):
                    snap.table_tags[row["table_name"]] = json.loads(row["tags"] or "[]")
                tables_json = self._meta(conn, "tables")
                if tables_json:
                    snap.tables = json.loads(tables_json)
                for row in conn.execute("SELECT doc_id, vec FROM embeddings"):
                    snap.vec[row["doc_id"]] = _f32_list(row["vec"])
                for row in conn.execute("SELECT table_name, vec FROM table_embeddings"):
                    snap.table_vec[row["table_name"]] = _f32_list(row["vec"])
                schema_json = self._meta(conn, "schema")
                if schema_json:
                    snap.schema = json.loads(schema_json)
                samples_json = self._meta(conn, "samples")
                if samples_json:
                    snap.samples = json.loads(samples_json)
                llm_edges_json = self._meta(conn, "llm_graph_edges")
                if llm_edges_json:
                    snap.llm_graph_edges = json.loads(llm_edges_json)
                llm_tombstones_json = self._meta(conn, "llm_edge_tombstones")
                if llm_tombstones_json:
                    snap.llm_edge_tombstones = json.loads(llm_tombstones_json)
            finally:
                conn.close()
        except Exception as e:
            logger.warning("[kb.storage] %s SQLite artifact 读取失败（按空库处理）：%s", self._path.name, e)
        return snap

    @staticmethod
    def _meta(conn: Any, key: str) -> str:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else ""

    # ---- 保存 ----
    def save(self, snap: KbSnapshot) -> None:
        with self._lock:
            conn = self._conn()
            try:
                self._init(conn)
                conn.execute("DELETE FROM docs")
                conn.execute("DELETE FROM edges")
                conn.execute("DELETE FROM tags")
                conn.execute("DELETE FROM table_tags")
                conn.execute("DELETE FROM embeddings")
                conn.execute("DELETE FROM table_embeddings")
                conn.execute("DELETE FROM meta WHERE key NOT IN ('kb_version', 'prev_stash')")  # 版本机制 key 由专属方法管理，不随快照重建
                for d in snap.auto + snap.user:
                    conn.execute(
                        "INSERT INTO docs (id, kind, title, body, table_name, column_name, status, source, tags, conn_id, updated_at, archived) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (d.get("id"), d.get("kind"), d.get("title"), d.get("body"),
                         d.get("table"), d.get("column"), d.get("status"), d.get("source", "auto"),
                         json.dumps(d.get("tags") or [], ensure_ascii=False),
                         d.get("conn_id"), d.get("updated_at", ""), int(bool(d.get("archived")))),
                    )
                for e in snap.edges:
                    conn.execute(
                        "INSERT INTO edges (from_table, from_col, to_table, to_col, kind, weight, shared, cardinality, reason) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (e.get("from"), e.get("from_col"), e.get("to"), e.get("to_col"),
                         e.get("kind"), e.get("weight"), e.get("shared"),
                         e.get("cardinality") or "n:1", e.get("reason") or ""),
                    )
                for name, v in snap.tags.items():
                    conn.execute("INSERT INTO tags (name, description, status, color) VALUES (?,?,?,?)",
                                 (name, v.get("description", ""), v.get("status", "draft"), v.get("color", "") or None))
                for table, names in snap.table_tags.items():
                    conn.execute("INSERT INTO table_tags (table_name, tags) VALUES (?,?)",
                                 (table, json.dumps(names, ensure_ascii=False)))
                for doc_id, vec in snap.vec.items():
                    conn.execute("INSERT INTO embeddings (doc_id, vec) VALUES (?,?)", (doc_id, _f32_blob(vec)))
                for table, vec in snap.table_vec.items():
                    conn.execute("INSERT INTO table_embeddings (table_name, vec) VALUES (?,?)", (table, _f32_blob(vec)))
                conn.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (str(KB_SNAPSHOT_VERSION),))
                conn.execute("INSERT INTO meta (key, value) VALUES ('emb_fingerprint', ?)", (snap.emb_fingerprint,))
                conn.execute("INSERT INTO meta (key, value) VALUES ('schema_fingerprint', ?)", (snap.schema_fingerprint,))
                conn.execute("INSERT INTO meta (key, value) VALUES ('synced_at', ?)", (snap.synced_at,))
                conn.execute("INSERT INTO meta (key, value) VALUES ('edge_tombstones', ?)",
                             (json.dumps(snap.edge_tombstones, ensure_ascii=False),))
                conn.execute("INSERT INTO meta (key, value) VALUES ('excluded_tables', ?)",
                             (json.dumps(snap.excluded, ensure_ascii=False),))
                conn.execute("INSERT INTO meta (key, value) VALUES ('llm_graph_edges', ?)",
                             (json.dumps(snap.llm_graph_edges, ensure_ascii=False),))
                conn.execute("INSERT INTO meta (key, value) VALUES ('llm_edge_tombstones', ?)",
                             (json.dumps(snap.llm_edge_tombstones, ensure_ascii=False),))
                conn.execute("INSERT INTO meta (key, value) VALUES ('schema', ?)", (json.dumps(snap.schema, ensure_ascii=False),))
                conn.execute("INSERT INTO meta (key, value) VALUES ('samples', ?)", (json.dumps(snap.samples, ensure_ascii=False),))
                conn.execute("INSERT INTO meta (key, value) VALUES ('tables', ?)", (json.dumps(snap.tables, ensure_ascii=False),))
                # vec0 虚拟表同步（sqlite-vec 可用时；维度 256，超长截断由 embedder 维度决定）
                if self._vec_ok:
                    conn.execute("DELETE FROM doc_vec")
                    conn.execute("DELETE FROM table_vec")
                    def _pad256(vec: list[float]) -> bytes:
                        v = (vec[:256] + [0.0] * 256)[:256]  # 截断超长 / 补齐不足
                        return _f32_blob(v)
                    for doc_id, vec in snap.vec.items():
                        conn.execute("INSERT INTO doc_vec (doc_id, vec) VALUES (?,?)", (doc_id, _pad256(vec)))
                    for table, vec in snap.table_vec.items():
                        conn.execute("INSERT INTO table_vec (table_name, vec) VALUES (?,?)", (table, _pad256(vec)))
                conn.commit()
            finally:
                conn.close()

    # ---- 查询表达力：标准 SQL ----
    def hop_sql(self, table: str, hops: int) -> str:
        """k-hop 邻居的标准递归 CTE（与上层 expand_tables 等价，供审计/服务化/企业版复用）。"""
        depth = max(1, int(hops))
        return f"""WITH RECURSIVE reach(name, depth) AS (
  SELECT '{table}', 0
  UNION
  SELECT e.to_table, r.depth + 1 FROM edges e JOIN reach r ON e.from_table = r.name
    WHERE e.kind = 'fk' AND r.depth < {depth}
  UNION
  SELECT e.from_table, r.depth + 1 FROM edges e JOIN reach r ON e.to_table = r.name
    WHERE e.kind = 'fk' AND r.depth < {depth}
)
SELECT DISTINCT name FROM reach ORDER BY name;"""

    def vec_topn_sql(self, k: int) -> str:
        """向量 top-N 标准查询（vec0 虚拟表，sqlite-vec 可用时）。返回 SQL 模板，参数 q = float32 BLOB。"""
        if not self._vec_ok:
            raise NotImplementedError("sqlite-vec 不可用，无法提供 vec0 SQL 查询")
        return f"SELECT doc_id, distance FROM doc_vec WHERE vec MATCH ? ORDER BY distance LIMIT {max(1, int(k))};"

    # ---- 版本机制（版本制知识库）：版本号 / 当前版本备份 / 字段级历史归档 ----

    def get_kb_version(self) -> int:
        with self._lock:
            con = self._conn()
            try:
                row = con.execute("SELECT value FROM meta WHERE key='kb_version'").fetchone()
                return int(row[0]) if row else 0
            finally:
                con.close()

    def bump_kb_version(self) -> int:
        """版本号 +1 并返回新值（单调递增，放弃不消耗）。"""
        with self._lock:
            con = self._conn()
            try:
                cur = con.execute("SELECT value FROM meta WHERE key='kb_version'")
                row = cur.fetchone()
                v = (int(row[0]) if row else 0) + 1
                con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('kb_version', ?)", (str(v),))
                con.commit()
                return v
            finally:
                con.close()

    def save_stash(self, data: dict[str, Any]) -> None:
        """当前版本全量备份落盘（放弃回滚源；pending 期间刷新不丢）。"""
        with self._lock:
            con = self._conn()
            try:
                self._init(con)
                con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('prev_stash', ?)",
                            (json.dumps(data, ensure_ascii=False),))
                con.commit()
            finally:
                con.close()

    def load_stash(self) -> dict[str, Any] | None:
        with self._lock:
            con = self._conn()
            try:
                row = con.execute("SELECT value FROM meta WHERE key='prev_stash'").fetchone()
                if not row:
                    return None
                return json.loads(row[0])
            except Exception:
                return None
            finally:
                con.close()

    def clear_stash(self) -> None:
        with self._lock:
            con = self._conn()
            try:
                con.execute("DELETE FROM meta WHERE key='prev_stash'")
                con.commit()
            finally:
                con.close()

    def archive_fields(self, batch_ts: str, version: int, rows: list[dict[str, Any]]) -> int:
        """字段级历史归档：rows = [{kind, table, column, payload}]（kind: column|table）。"""
        if not rows:
            return 0
        with self._lock:
            con = self._conn()
            try:
                con.executemany(
                    "INSERT INTO version_archive (batch_ts, version, kind, table_name, column_name, payload) "
                    "VALUES (?,?,?,?,?,?)",
                    [(batch_ts, version, r["kind"], r["table"], r.get("column", ""),
                      json.dumps(r["payload"], ensure_ascii=False)) for r in rows],
                )
                con.commit()
                return len(rows)
            finally:
                con.close()

    def field_history(self, table: str, column: str) -> list[dict[str, Any]]:
        """某字段的历史版本（倒序）：[{id, batch_ts, version, comment, values, example, status}]。"""
        with self._lock:
            con = self._conn()
            try:
                rows = con.execute(
                    "SELECT id, batch_ts, version, payload FROM version_archive "
                    "WHERE kind='column' AND table_name=? AND column_name=? "
                    "ORDER BY version DESC, batch_ts DESC",
                    (table, column),
                ).fetchall()
                out = []
                for r in rows:
                    p = json.loads(r["payload"])
                    out.append({
                        "id": r["id"], "batch_ts": r["batch_ts"], "version": r["version"],
                        "comment": p.get("comment", ""), "values": p.get("values", ""),
                        "example": p.get("example", ""), "status": p.get("status", ""),
                    })
                return out
            finally:
                con.close()

    def table_history(self, table: str) -> list[dict[str, Any]]:
        """某表注释的历史版本（倒序）：[{id, batch_ts, version, comment, ddl, vector_override}]。"""
        with self._lock:
            con = self._conn()
            try:
                rows = con.execute(
                    "SELECT id, batch_ts, version, payload FROM version_archive "
                    "WHERE kind='table' AND table_name=? "
                    "ORDER BY version DESC, batch_ts DESC",
                    (table,),
                ).fetchall()
                out = []
                for r in rows:
                    p = json.loads(r["payload"])
                    out.append({
                        "id": r["id"], "batch_ts": r["batch_ts"], "version": r["version"],
                        "comment": p.get("comment", ""), "ddl": p.get("ddl", ""),
                        "vector_override": p.get("vector_override", ""),
                    })
                return out
            finally:
                con.close()

    def archive_row(self, row_id: int) -> dict[str, Any] | None:
        with self._lock:
            con = self._conn()
            try:
                row = con.execute(
                    "SELECT id, kind, table_name, column_name, payload FROM version_archive WHERE id=?",
                    (int(row_id),),
                ).fetchone()
                if not row:
                    return None
                return {"kind": row["kind"], "table": row["table_name"],
                        "column": row["column_name"], "payload": json.loads(row["payload"])}
            finally:
                con.close()

    def trim_archive(self, keep_batches: int = 3) -> int:
        """物理删除早于最近 keep_batches 个批次（batch_ts 粒度）的历史行，返回删除数。"""
        keep = max(1, int(keep_batches))
        with self._lock:
            con = self._conn()
            try:
                batches = [r[0] for r in con.execute(
                    "SELECT DISTINCT batch_ts FROM version_archive ORDER BY batch_ts DESC LIMIT ?",
                    (keep,),
                ).fetchall()]
                if not batches:
                    return 0
                marks = ",".join("?" for _ in batches)
                cur = con.execute(
                    f"DELETE FROM version_archive WHERE batch_ts NOT IN ({marks})", batches
                )
                con.commit()
                return cur.rowcount
            finally:
                con.close()


def make_storage(data_dir: Path, conn_id: str, backend: str = "") -> KbStorage:
    """按配置选后端：sqlite（默认，sqlite-vec 探测失败自动回退 json）| json。"""
    choice = (backend or os.environ.get("TABLETALK_KB_STORAGE", "") or "sqlite").lower()
    if choice == "json":
        return JsonStorage(data_dir, conn_id)
    store = SqliteStorage(data_dir, conn_id)
    return store
