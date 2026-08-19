"""存储后端：JsonStorage / SqliteStorage 双实现一致性 + 旧 JSON 迁移 + 标准 SQL 查询接口。"""

from __future__ import annotations

import sqlite3
import struct

from app.knowledge.storage import JsonStorage, KbSnapshot, SqliteStorage, make_storage, _f32_blob


def _snap() -> KbSnapshot:
    return KbSnapshot(
        auto=[{"id": "auto-1", "conn_id": "c1", "kind": "table", "title": "orders", "body": "订单主表",
               "table": "orders", "column": None, "tags": ["订单"], "source": "auto", "status": "confirmed",
               "updated_at": "2026-08-19T00:00:00"}],
        drafts=[{"id": "ai-tbl-orders", "conn_id": "c1", "kind": "table", "title": "orders", "body": "AI 草案",
                 "table": "orders", "column": None, "tags": [], "source": "ai_draft", "status": "draft", "updated_at": ""}],
        user=[{"id": "usr-1", "conn_id": "c1", "kind": "note", "title": "orders", "body": "手写注释",
               "table": "orders", "column": None, "tags": [], "source": "user", "status": "confirmed", "updated_at": ""}],
        edges=[{"from": "orders", "from_col": "customer_id", "to": "customers", "to_col": "id",
                "kind": "fk", "weight": None, "shared": None}],
        vec={"auto-1": [1.0, 0.0, 0.5]},
        table_vec={"orders": [0.2, 0.8, 0.1]},
        tags={"订单": {"description": "交易领域", "status": "confirmed"}},
        table_tags={"orders": ["订单"]},
        schema={"tables": [{"name": "orders"}], "columns": [], "foreign_keys": []},
        emb_fingerprint="hash",
    )


def test_sqlite_roundtrip(tmp_path):
    st = SqliteStorage(tmp_path, "c1")
    st.save(_snap())
    assert st.exists()
    snap2 = st.load()
    assert [d["id"] for d in snap2.auto] == ["auto-1"]
    assert [d["body"] for d in snap2.drafts] == ["AI 草案"]
    assert [d["body"] for d in snap2.user] == ["手写注释"]
    assert snap2.edges[0]["from"] == "orders" and snap2.edges[0]["to"] == "customers"
    assert snap2.vec["auto-1"] == [1.0, 0.0, 0.5]
    # float32 存储有精度误差：逐项近似
    tv = snap2.table_vec["orders"]
    assert len(tv) == 3 and all(abs(a - b) < 1e-5 for a, b in zip(tv, [0.2, 0.8, 0.1]))
    assert snap2.tags["订单"]["status"] == "confirmed"
    assert snap2.table_tags["orders"] == ["订单"]
    assert snap2.emb_fingerprint == "hash"
    # 标准格式：SQLite 工具可直接读
    conn = sqlite3.connect(str(tmp_path / "knowledge-c1.db"))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"docs", "edges", "tags", "embeddings", "meta"} <= tables
    assert conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 3


def test_json_and_sqlite_roundtrip_equivalent(tmp_path):
    js = JsonStorage(tmp_path, "c1")
    js.save(_snap())
    j2 = JsonStorage(tmp_path, "c1").load()
    st = SqliteStorage(tmp_path, "c1")
    st.save(_snap())
    s2 = st.load()
    assert sorted(d["id"] for d in j2.auto) == sorted(d["id"] for d in s2.auto)
    assert j2.edges == s2.edges
    assert j2.vec == s2.vec
    assert j2.tags == s2.tags
    assert j2.table_tags == s2.table_tags


def test_migrate_from_legacy_json(tmp_path):
    """旧 JSON artifact → SqliteStorage 首次 load 自动迁移。"""
    js = JsonStorage(tmp_path, "c1")
    js.save(_snap())
    assert (tmp_path / "knowledge-c1.json").exists()
    st = SqliteStorage(tmp_path, "c1")
    assert not st.exists()
    snap = st.load()  # 触发迁移
    assert st.exists()  # 迁移后 .db 已建
    assert [d["id"] for d in snap.auto] == ["auto-1"]
    assert snap.edges[0]["kind"] == "fk"


def test_hop_sql_recursive_cte_matches_bfs(tmp_path):
    """查询表达力：递归 CTE k-hop 与上层 expand_tables 语义一致。"""
    from app.knowledge.store import KnowledgeBase
    st = SqliteStorage(tmp_path, "c1")
    st.save(_snap())
    kb = KnowledgeBase(tmp_path)
    kb._load_conn("c1")
    bfs = kb.expand_tables("c1", {"orders"}, hops=1)
    sql = kb.hop_sql("c1", "orders", hops=1)
    assert "RECURSIVE" in sql and "edges" in sql
    conn = sqlite3.connect(str(tmp_path / "knowledge-c1.db"))
    rows = conn.execute(sql).fetchall()
    sql_tables = {r[0] for r in rows}
    assert sql_tables == bfs


def test_vec_topn_sql_returns_topk(tmp_path):
    """查询表达力：vec0 标准 SQL 的 top-N 与 BatchIndex 结果一致（sqlite-vec 可用时）。"""
    st = SqliteStorage(tmp_path, "c1")
    if not st.vec_available():
        return  # 环境无 sqlite-vec：跳过
    snap = _snap()
    snap.vec = {"a": [1.0, 0.0, 0.0], "b": [0.0, 1.0, 0.0], "c": [0.9, 0.1, 0.0]}
    st.save(snap)
    sql = st.vec_topn_sql(2)
    conn = sqlite3.connect(str(tmp_path / "knowledge-c1.db"))
    conn.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(conn)
    q = _f32_blob(([1.0, 0.0, 0.0] + [0.0] * 256)[:256])
    rows = conn.execute(sql, (q,)).fetchall()
    assert rows[0][0] == "a"
    assert len(rows) == 2


def test_make_storage_backend_choice(tmp_path):
    assert make_storage(tmp_path, "c1", "json").kind == "json"
    assert make_storage(tmp_path, "c1", "sqlite").kind == "sqlite"
    assert make_storage(tmp_path, "c1", "").kind == "sqlite"  # 默认 sqlite
