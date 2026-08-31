"""T2 统一图谱测试：graph_read/graph_write 指向知识库唯一图（不再有独立 graph-{conn}.db）。"""
from __future__ import annotations

from pathlib import Path

from app.ai.tools.graph_read import _graph_read
from app.ai.tools.graph_write import _graph_write


def _schema() -> dict:
    return {
        "tables": [
            {"name": "orders", "kind": "table", "comment": "", "column_count": 2},
            {"name": "customers", "kind": "table", "comment": "客户表", "column_count": 2},
        ],
        "columns": [
            {"table": "orders", "name": "id", "type": "int", "nullable": True, "pk": True, "fk": False, "default": None, "comment": ""},
            {"table": "orders", "name": "customer_id", "type": "int", "nullable": True, "pk": False, "fk": True, "default": None, "comment": ""},
            {"table": "customers", "name": "id", "type": "int", "nullable": True, "pk": True, "fk": False, "default": None, "comment": ""},
            {"table": "customers", "name": "name", "type": "text", "nullable": True, "pk": False, "fk": False, "default": None, "comment": ""},
        ],
        "foreign_keys": [
            {"table": "orders", "column": "customer_id", "ref_table": "customers", "ref_column": "id"},
        ],
    }


async def test_graph_read_uses_kb_graph(app_state, conn_id, tmp_path):
    """graph_read（无参数）返回知识库图的边（FK），不是独立图的空边。"""
    kb = app_state.knowledge
    await kb.build(conn_id, _schema(), enable_ai_annotation=False)
    kb.confirm_graph_edges(conn_id)

    out = await _graph_read(app_state, {}, conn_id)
    assert out.result["ok"] is True
    edges = out.result["edges"]
    assert len(edges) == 1
    assert edges[0]["source"] == "orders"
    assert edges[0]["target"] == "customers"
    assert edges[0]["relation"] == "fk"


async def test_graph_read_with_table(app_state, conn_id):
    """graph_read 带表名 → 返回该表相关的关联边。"""
    kb = app_state.knowledge
    await kb.build(conn_id, _schema(), enable_ai_annotation=False)
    kb.confirm_graph_edges(conn_id)

    out = await _graph_read(app_state, {"table_name": "orders", "hops": 2}, conn_id)
    assert out.result["ok"] is True
    assert out.result["table"] == "orders"
    assert out.result["count"] == 1
    assert out.result["relations"][0]["target"] == "customers"


async def test_graph_write_visible_in_kb(app_state, conn_id):
    """graph_write 加的边 → 知识库图可见，graph_read 也能读到（同一数据源）。"""
    kb = app_state.knowledge
    await kb.build(conn_id, _schema(), enable_ai_annotation=False)

    out = await _graph_write(
        app_state,
        {"action": "add", "source": "orders", "target": "customers",
         "relation": "user", "source_col": "id", "target_col": "id"},
        conn_id,
    )
    assert out.result["ok"] is True

    # 知识库图可见
    edges = kb.graph(conn_id)["edges"]
    assert any(e["kind"] == "user" for e in edges)
    # graph_read 可见（同一数据源）
    out2 = await _graph_read(app_state, {}, conn_id)
    assert any(e["relation"] == "user" for e in out2.result["edges"])


async def test_graph_write_remove(app_state, conn_id):
    """graph_write remove → 边从知识库图删除。"""
    kb = app_state.knowledge
    await kb.build(conn_id, _schema(), enable_ai_annotation=False)
    kb.confirm_graph_edges(conn_id)

    out = await _graph_write(
        app_state,
        {"action": "remove", "source": "orders", "target": "customers", "relation": "fk"},
        conn_id,
    )
    assert out.result["ok"] is True
    edges = kb.graph(conn_id)["edges"]
    assert all(e["kind"] != "fk" for e in edges)


def test_no_separate_graph_db(app_state, conn_id, tmp_path):
    """统一图谱后不再产生独立的 graph-{conn}.db。"""
    data_dir = Path(str(tmp_path / "data"))
    assert not list(data_dir.glob("graph-*.db")), "不应存在独立 graph-{conn}.db"
