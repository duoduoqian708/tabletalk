"""图 diff 回归（2026-09）：全量重建去重吞边 → diff 基线误判全红。

根因：set_diff_base 误传"去重后队列"——与已确认边完全一致而被丢弃的 raw 提案
既不在队列也不在基线，被 graph() 判为"本轮未重新提案"（removed）。
修复后基线 = 本轮 raw 提案集合（build.py 传参），与已确认一致的边不再判红。
"""
from __future__ import annotations

from app.knowledge.graph.store import GraphStore


def _schema():
    return {
        "tables": [{"name": "t_a"}, {"name": "t_b"}],
        "columns": [
            {"table": "t_a", "name": "id"}, {"table": "t_a", "name": "b_id"},
            {"table": "t_b", "name": "id"},
        ],
        "foreign_keys": [{"table": "t_a", "column": "b_id", "ref_table": "t_b", "ref_column": "id"}],
    }


def _fk_draft():
    return {"from_table": "t_a", "from_col": "b_id", "to_table": "t_b", "to_col": "id",
            "source": "fk", "cardinality": "n:1"}


def _seed_confirmed(store: GraphStore, conn: str, schema: dict) -> None:
    """已确认 FK 边（上一轮产物，重建后仍应存活且不判红）。"""
    store.add_graph_edge(conn, "t_a", "t_b", "fk", frm_col="b_id", to_col="id",
                         cardinality="n:1", schema=schema)


def test_rebuild_identical_fk_edge_not_marked_removed():
    """全量重建：raw draft 与已确认边完全一致 → 去重丢弃，但基线含 raw 键 → 不判红。"""
    store = GraphStore()
    conn = "c1"
    _seed_confirmed(store, conn, _schema())
    schema = _schema()
    store.filter_confirmed_by_schema(conn, schema)

    drafts = store.build_draft_edges(schema)          # raw 产出（本轮重新主张）
    n = store.merge_draft_edges(conn, drafts)          # 与已确认一致 → 去重丢弃
    assert n == 0
    # 修复后的调用（build.py）：基线 = raw 提案 ∪ 队列
    store.set_diff_base(conn, [*drafts, *store.llm_graph_edges(conn)])

    g = store.graph(conn, schema)
    confirmed = [e for e in g["edges"] if e["from"] == "t_a"]
    assert confirmed, "已确认边应存活"
    assert all(e["diff"] is None for e in confirmed), "与本轮 raw 提案一致的已确认边不得判红"
    assert g["llm_draft_edges"] == []


def test_rebuild_missing_repropose_still_red():
    """反向：已确认边本轮推导不出（FK 已删）→ 仍判红（removed）。"""
    store = GraphStore()
    conn = "c1"
    _seed_confirmed(store, conn, _schema())
    schema = {**_schema(), "foreign_keys": []}        # FK 被删 → 推导不出
    store.filter_confirmed_by_schema(conn, schema)

    drafts = store.build_draft_edges(schema)
    store.merge_draft_edges(conn, drafts)
    store.set_diff_base(conn, [*drafts, *store.llm_graph_edges(conn)])

    g = store.graph(conn, schema)
    confirmed = [e for e in g["edges"] if e["from"] == "t_a"]
    assert confirmed and all(e["diff"] == "removed" for e in confirmed)


def test_modified_cardinality_stays_draft():
    """同列对不同基数 → 留在 draft 队列（modified），不判红已确认边。"""
    store = GraphStore()
    conn = "c1"
    _seed_confirmed(store, conn, _schema())
    schema = _schema()
    drafts = [{**_fk_draft(), "cardinality": "1:1"}]
    store.merge_draft_edges(conn, drafts)
    store.set_diff_base(conn, [*drafts, *store.llm_graph_edges(conn)])

    g = store.graph(conn, schema)
    confirmed = [e for e in g["edges"] if e["from"] == "t_a"]
    assert all(e["diff"] is None for e in confirmed)
    assert len(g["llm_draft_edges"]) == 1 and g["llm_draft_edges"][0]["diff"] == "modified"
