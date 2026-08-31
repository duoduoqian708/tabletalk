"""T3 边模型升级测试：GraphEdge（列对列表/守卫/置信度）+ 存储读写。"""
from __future__ import annotations

import pytest

from app.knowledge.facade import KnowledgeBase
from app.knowledge.graph.model import GraphEdge


def _schema_two_fks() -> dict:
    """orders 与 users 之间有两条平行 FK（user_id / shipped_by）。"""
    return {
        "tables": [
            {"name": "orders", "kind": "table", "comment": "", "column_count": 3},
            {"name": "users", "kind": "table", "comment": "", "column_count": 2},
        ],
        "columns": [
            {"table": "orders", "name": "id", "type": "int", "nullable": True, "pk": True, "fk": False, "default": None, "comment": ""},
            {"table": "orders", "name": "user_id", "type": "int", "nullable": True, "pk": False, "fk": True, "default": None, "comment": ""},
            {"table": "orders", "name": "shipped_by", "type": "int", "nullable": True, "pk": False, "fk": True, "default": None, "comment": ""},
            {"table": "users", "name": "id", "type": "int", "nullable": True, "pk": True, "fk": False, "default": None, "comment": ""},
            {"table": "users", "name": "name", "type": "text", "nullable": True, "pk": False, "fk": False, "default": None, "comment": ""},
        ],
        "foreign_keys": [
            {"table": "orders", "column": "user_id", "ref_table": "users", "ref_column": "id"},
            {"table": "orders", "column": "shipped_by", "ref_table": "users", "ref_column": "id"},
        ],
    }


def _schema_self_fk() -> dict:
    """自引用：employee.manager_id → employee.id。"""
    return {
        "tables": [{"name": "employee", "kind": "table", "comment": "", "column_count": 2}],
        "columns": [
            {"table": "employee", "name": "id", "type": "int", "nullable": True, "pk": True, "fk": False, "default": None, "comment": ""},
            {"table": "employee", "name": "manager_id", "type": "int", "nullable": True, "pk": False, "fk": True, "default": None, "comment": ""},
        ],
        "foreign_keys": [
            {"table": "employee", "column": "manager_id", "ref_table": "employee", "ref_column": "id"},
        ],
    }


# ---------- GraphEdge 模型 ----------

def test_composite_condition():
    e = GraphEdge(source_table="orders", target_table="users",
                  cols=[("order_id", "id"), ("line_no", "line_no")])
    assert e.join_condition() == "orders.order_id = users.id AND orders.line_no = users.line_no"


def test_guarded_condition():
    e = GraphEdge(source_table="x", target_table="table1",
                  cols=[("code", "code")], guard="x.type = 1")
    assert e.join_condition().endswith(" AND x.type = 1")


def test_single_col_condition():
    e = GraphEdge(source_table="orders", target_table="users", cols=[("user_id", "id")])
    assert e.join_condition() == "orders.user_id = users.id"


def test_empty_cols_rejected():
    with pytest.raises(ValueError):
        GraphEdge(source_table="a", target_table="b", cols=[])


def test_dict_roundtrip():
    e = GraphEdge(source_table="x", target_table="t1", cols=[("code", "code")],
                  guard="x.type = 1", confidence=0.5, provenance="value_overlap")
    d = e.to_dict()
    e2 = GraphEdge.from_dict(d)
    assert e2.guard == "x.type = 1"
    assert e2.confidence == 0.5
    assert e2.provenance == "value_overlap"
    assert e2.cols == [("code", "code")]


# ---------- 构建产物（FK 边带新字段 + 平行边共存） ----------

async def test_build_fk_edges_have_new_fields(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema_two_fks(), enable_ai_annotation=False)
    kb.confirm_graph_edges("c1")
    edges = kb.graph("c1")["edges"]
    assert len(edges) == 2  # 平行边共存，不被合并
    for e in edges:
        assert e["confidence"] == 1.0
        assert e["provenance"] == "declared_fk"
        assert e["guard"] is None


async def test_parallel_edges_coexist(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema_two_fks(), enable_ai_annotation=False)
    kb.confirm_graph_edges("c1")
    edges = kb.graph("c1")["edges"]
    cols = {(e["from_col"], e["to_col"]) for e in edges}
    assert ("user_id", "id") in cols
    assert ("shipped_by", "id") in cols


async def test_self_loop_edge(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema_self_fk(), enable_ai_annotation=False)
    kb.confirm_graph_edges("c1")
    edges = kb.graph("c1")["edges"]
    assert len(edges) == 1
    e = edges[0]
    assert e["from"] == "employee" and e["to"] == "employee"  # 自环可存储


async def test_guarded_edge_persists(tmp_path):
    """守卫边经存储往返不丢 guard。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema_two_fks(), enable_ai_annotation=False)
    kb.add_graph_edge("c1", "orders", "users", "user",
                      frm_col="status", to_col="id")
    # 手动加一条带 guard 的边（dict 直写模拟守卫边）
    kb.graph_store._graph["c1"]["edges"].append({
        "from": "orders", "from_col": "code", "to": "users", "to_col": "code",
        "kind": "value_overlap", "weight": 1.0, "shared": None,
        "cardinality": "n:1", "reason": "判别器",
        "guard": "orders.type = 1", "confidence": 0.5,
        "provenance": "value_overlap", "cols": None,
    })
    kb._save_conn("c1")

    kb2 = KnowledgeBase(tmp_path)
    kb2._load_conn("c1")
    edges = kb2.graph("c1")["edges"]
    guarded = [e for e in edges if e.get("guard")]
    assert len(guarded) == 1
    assert guarded[0]["guard"] == "orders.type = 1"
    assert guarded[0]["confidence"] == 0.5


# ---------- T3 文档补测：旧库迁移 / 存储往返 / 守卫边共存 ----------

def _write_old_schema_db(path, edges_rows, version=None):
    """手写一个旧 schema 库（edges 无 guard/confidence/provenance/cols 列）。"""
    import sqlite3
    from app.knowledge.storage import KB_SNAPSHOT_VERSION

    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('version', ?)",
                 (str(version if version is not None else KB_SNAPSHOT_VERSION),))
    conn.execute(
        "CREATE TABLE edges (from_table TEXT, from_col TEXT, to_table TEXT, to_col TEXT, "
        "kind TEXT, weight REAL, shared INTEGER, cardinality TEXT, reason TEXT)"
    )
    for r in edges_rows:
        conn.execute(
            "INSERT INTO edges (from_table, from_col, to_table, to_col, kind, weight, shared, cardinality, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?)", r)
    conn.execute("CREATE TABLE tags (name TEXT PRIMARY KEY, description TEXT, status TEXT, color TEXT)")
    conn.execute("CREATE TABLE table_tags (table_name TEXT PRIMARY KEY, tags TEXT)")
    conn.execute(
        "CREATE TABLE docs (id TEXT PRIMARY KEY, kind TEXT, title TEXT, body TEXT, table_name TEXT, "
        "column_name TEXT, status TEXT, source TEXT, tags TEXT, conn_id TEXT, updated_at TEXT, "
        "archived INTEGER DEFAULT 0)")
    conn.commit()
    conn.close()


async def test_single_col_migration(tmp_path):
    """旧 schema 库读入：cols 合成列对、kind 映射 provenance（fk→declared_fk / user→human）。"""
    from app.knowledge.storage import SqliteStorage

    db = tmp_path / "knowledge-c1.db"
    _write_old_schema_db(db, [
        ("orders", "user_id", "users", "id", "fk", 1.0, None, "n:1", ""),
        ("a", "x", "b", "y", "user", 1.0, None, "n:1", ""),
    ])
    snap = SqliteStorage(tmp_path, "c1").load()
    assert len(snap.edges) == 2
    by_kind = {e["kind"]: e for e in snap.edges}
    fk = by_kind["fk"]
    assert fk["cols"] == [["user_id", "id"]]
    assert fk["provenance"] == "declared_fk"
    assert fk["confidence"] == 1.0
    user = by_kind["user"]
    assert user["provenance"] == "human"
    assert user["cols"] == [["x", "y"]]


async def test_composite_cols_roundtrip(tmp_path):
    """复合列对（cols JSON）经 save→load 无损（修 T3 往返损坏 bug）。"""
    kb = KnowledgeBase(tmp_path)
    kb.graph_store._graph["c1"] = {"edges": [{
        "from": "orders", "from_col": "order_id", "to": "users", "to_col": "id",
        "kind": "fk", "weight": 1.0, "shared": None, "cardinality": "n:1", "reason": "",
        "guard": None, "confidence": 1.0, "provenance": "declared_fk",
        "cols": [["order_id", "id"], ["line_no", "line_no"]],
    }]}
    kb._save_conn("c1")
    kb2 = KnowledgeBase(tmp_path)
    kb2._load_conn("c1")
    e = kb2.graph("c1")["edges"][0]
    assert e["cols"] == [["order_id", "id"], ["line_no", "line_no"]]
    assert isinstance(e["cols"], list)  # 不是 JSON 字符串


async def test_self_loop_roundtrip(tmp_path):
    """自环边 put→get 存储往返一致。"""
    kb = KnowledgeBase(tmp_path)
    kb.graph_store._graph["c1"] = {"edges": [{
        "from": "employee", "from_col": "manager_id", "to": "employee", "to_col": "id",
        "kind": "fk", "weight": 1.0, "shared": None, "cardinality": "n:1", "reason": "",
        "guard": None, "confidence": 1.0, "provenance": "declared_fk", "cols": None,
    }]}
    kb._save_conn("c1")
    kb2 = KnowledgeBase(tmp_path)
    kb2._load_conn("c1")
    edges = kb2.graph("c1")["edges"]
    assert len(edges) == 1
    e = edges[0]
    assert e["from"] == "employee" and e["to"] == "employee"
    assert e["cols"] == [["manager_id", "id"]]  # 旧列对合成


async def test_guarded_edges_coexist(tmp_path):
    """同 (表对,列对) 不同 guard 的两条边共存（多重图不被去重吞掉）。"""
    kb = KnowledgeBase(tmp_path)
    kb.graph_store._graph["c1"] = {"edges": [
        {"from": "x", "from_col": "code", "to": "t1", "to_col": "code",
         "kind": "value_overlap", "weight": 1.0, "shared": None, "cardinality": "n:1",
         "reason": "", "guard": "x.type = 1", "confidence": 0.5,
         "provenance": "value_overlap", "cols": [["code", "code"]]},
        {"from": "x", "from_col": "code", "to": "t2", "to_col": "code",
         "kind": "value_overlap", "weight": 1.0, "shared": None, "cardinality": "n:1",
         "reason": "", "guard": "x.type = 2", "confidence": 0.5,
         "provenance": "value_overlap", "cols": [["code", "code"]]},
    ]}
    kb._save_conn("c1")
    kb2 = KnowledgeBase(tmp_path)
    kb2._load_conn("c1")
    edges = kb2.graph("c1")["edges"]
    guards = sorted(e["guard"] for e in edges)
    assert guards == ["x.type = 1", "x.type = 2"]
