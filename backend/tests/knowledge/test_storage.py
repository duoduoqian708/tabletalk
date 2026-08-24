"""存储后端 v2：JsonStorage / SqliteStorage 双实现一致性 + 快照 v2 往返 + 旧工件作废门控。"""

from __future__ import annotations

import json
import sqlite3
import struct

from app.knowledge.storage import KB_SNAPSHOT_VERSION, JsonStorage, KbSnapshot, SqliteStorage, make_storage, _f32_blob


def _snap() -> KbSnapshot:
    return KbSnapshot(
        tables={
            "orders": {
                "name": "orders", "db_comment": "订单主表", "column_count": 2,
                "comment": "订单业务实体", "status": "draft",
                "columns": {
                    "id": {"name": "id", "type": "int", "pk": True, "fk": False,
                           "db_comment": "", "comment": "订单ID", "values": "",
                           "example": "123", "status": "confirmed"},
                    "status": {"name": "status", "type": "varchar(16)", "pk": False, "fk": False,
                               "db_comment": "", "comment": "订单状态",
                               "values": "P=待付款；S=已发货", "example": "P", "status": "draft"},
                },
                "ddl": "CREATE TABLE orders (id INTEGER PRIMARY KEY)",
                "excluded": False,
            },
        },
        auto=[{"id": "auto-1", "conn_id": "c1", "kind": "table", "title": "orders", "body": "订单主表",
               "table": "orders", "column": None, "tags": ["订单"], "source": "auto", "status": "confirmed",
               "updated_at": "2026-08-19T00:00:00"}],
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


def test_sqlite_roundtrip_v2(tmp_path):
    st = SqliteStorage(tmp_path, "c1")
    st.save(_snap())
    assert st.exists()
    snap2 = st.load()
    assert snap2.version == KB_SNAPSHOT_VERSION
    # 表级知识往返（含逐列 ColumnInfo 全字段）
    assert set(snap2.tables) == {"orders"}
    tk = snap2.tables["orders"]
    assert tk["db_comment"] == "订单主表"
    assert tk["comment"] == "订单业务实体" and tk["status"] == "draft"
    assert tk["columns"]["id"]["pk"] is True and tk["columns"]["id"]["status"] == "confirmed"
    assert tk["columns"]["status"]["values"] == "P=待付款；S=已发货"
    assert tk["columns"]["status"]["example"] == "P"
    assert tk["ddl"].startswith("CREATE TABLE")
    # 文档/图/标签/向量沿用字段
    assert [d["id"] for d in snap2.auto] == ["auto-1"]
    assert [d["body"] for d in snap2.user] == ["手写注释"]
    assert snap2.edges[0]["from"] == "orders" and snap2.edges[0]["to"] == "customers"
    assert snap2.vec["auto-1"] == [1.0, 0.0, 0.5]
    # float32 存储有精度误差：逐项近似
    tv = snap2.table_vec["orders"]
    assert len(tv) == 3 and all(abs(a - b) < 1e-5 for a, b in zip(tv, [0.2, 0.8, 0.1]))
    assert snap2.tags["订单"]["status"] == "confirmed"
    assert snap2.table_tags["orders"] == ["订单"]
    assert snap2.emb_fingerprint == "hash"
    # 标准格式：SQLite 工具可直接读；meta 含版本号与 tables JSON
    conn = sqlite3.connect(str(tmp_path / "knowledge-c1.db"))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"docs", "edges", "tags", "embeddings", "meta"} <= tables
    assert conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 2
    meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
    assert meta["version"] == "2"
    assert json.loads(meta["tables"])["orders"]["name"] == "orders"


def test_json_and_sqlite_roundtrip_equivalent(tmp_path):
    js = JsonStorage(tmp_path, "c1")
    js.save(_snap())
    j2 = JsonStorage(tmp_path, "c1").load()
    st = SqliteStorage(tmp_path, "c1")
    st.save(_snap())
    s2 = st.load()
    assert j2.tables == s2.tables
    assert sorted(d["id"] for d in j2.auto) == sorted(d["id"] for d in s2.auto)
    assert j2.edges == s2.edges
    assert j2.vec == s2.vec
    assert j2.tags == s2.tags
    assert j2.table_tags == s2.table_tags


def _write_legacy_json(tmp_path) -> None:
    """手工写一份 v1 格式工件（无 version 字段：docs 碎片 + enums）。"""
    legacy = {
        "auto": [{"id": "auto-col-orders-status", "conn_id": "c1", "kind": "column",
                  "title": "orders.status", "body": "列碎片文档", "table": "orders",
                  "column": "status", "tags": [], "source": "auto", "status": "confirmed"}],
        "enums": {"orders": {"status": [{"value": "P", "meaning": "待付款", "status": "confirmed"}]}},
        "graph": {"edges": []},
        "vec": {}, "table_vec": {},
        "tags": {}, "table_tags": {}, "schema": {}, "samples": {},
    }
    (tmp_path / "knowledge-c1.json").write_text(json.dumps(legacy), encoding="utf-8")


def test_legacy_json_artifact_discarded_with_warning(tmp_path, caplog):
    """v1 JSON 工件（无 version）→ 按空库处理 + warning（旧工件作废不迁移）。"""
    import logging

    _write_legacy_json(tmp_path)
    with caplog.at_level(logging.WARNING, logger="app.knowledge.storage"):
        snap = JsonStorage(tmp_path, "c1").load()
    assert snap.tables == {}
    assert snap.auto == []
    assert not snap.edges
    assert any("旧工件作废，请重新构建" in r.message for r in caplog.records)


def test_legacy_sqlite_artifact_discarded_with_warning(tmp_path, caplog):
    """v1 SQLite 工件（meta 无 version）→ 按空库处理 + warning。"""
    import logging

    conn = sqlite3.connect(str(tmp_path / "knowledge-c1.db"))
    conn.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE docs (id TEXT PRIMARY KEY, kind TEXT, title TEXT, body TEXT,"
        " table_name TEXT, column_name TEXT, status TEXT, source TEXT, tags TEXT,"
        " conn_id TEXT, updated_at TEXT, archived INTEGER DEFAULT 0);"
        "INSERT INTO meta VALUES ('schema_fingerprint', 'abc');"
    )
    conn.commit()
    conn.close()
    with caplog.at_level(logging.WARNING, logger="app.knowledge.storage"):
        snap = SqliteStorage(tmp_path, "c1").load()
    assert snap == KbSnapshot()  # 全空快照
    assert any("旧工件作废，请重新构建" in r.message for r in caplog.records)


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
