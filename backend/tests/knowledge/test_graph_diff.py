"""图 diff 三色标记（2026-09）：重建提案 vs 当前生效边的 green/yellow/red。"""
from __future__ import annotations

from app.knowledge.graph.store import GraphStore


def _schema(tables, columns, fks=None):
    return {"tables": tables, "columns": columns, "foreign_keys": fks or []}


def _col(table, name, ctype="int", pk=False):
    return {"table": table, "name": name, "type": ctype, "pk": pk, "fk": False, "comment": ""}


def _conn_state():
    """预先放一条已确认 FK 边（orders.customer_id -> customers.id）+ schema。"""
    g = GraphStore()
    g._graph["c1"] = {"edges": [
        {"from": "orders", "from_col": "customer_id", "to": "customers", "to_col": "id",
         "kind": "fk", "cardinality": "n:1", "reason": "", "guard": None,
         "confidence": 1.0, "provenance": "declared_fk", "cols": [["customer_id", "id"]]},
        {"from": "orders", "from_col": "campaign_id", "to": "campaigns", "to_col": "id",
         "kind": "fk", "cardinality": "n:1", "reason": "", "guard": None,
         "confidence": 1.0, "provenance": "declared_fk", "cols": [["campaign_id", "id"]]},
    ]}
    schema = _schema(
        [{"name": "orders", "kind": "table", "comment": "", "column_count": 3},
         {"name": "customers", "kind": "table", "comment": "", "column_count": 1},
         {"name": "campaigns", "kind": "table", "comment": "", "column_count": 1},
         {"name": "regions", "kind": "table", "comment": "", "column_count": 1}],
        [_col("orders", "id", pk=True), _col("orders", "customer_id"),
         _col("orders", "campaign_id"), _col("orders", "region_id"),
         _col("customers", "id", pk=True), _col("campaigns", "id", pk=True),
         _col("regions", "id", pk=True)],
    )
    return g, schema


def test_removed_red_when_not_reproposed_endpoints_alive():
    """正式边本轮未重新提案、表字段仍存活 -> 红（removed）。"""
    g, schema = _conn_state()
    # 本轮重建重新提案了 customer FK 与 region naming；campaign_id 的 FK 约束被删
    # （列还在 -> 边未被重新提案、端点仍存活 -> 红）
    g.set_diff_base("c1", [
        {"from_table": "orders", "from_col": "customer_id", "to_table": "customers", "to_col": "id"},
        {"from_table": "orders", "from_col": "region_id", "to_table": "regions", "to_col": "id"},
    ])
    g._llm_graph_edges["c1"] = [{"from_table": "orders", "from_col": "region_id",
                                 "to_table": "regions", "to_col": "id", "kind": "naming",
                                 "cardinality": "n:1"}]
    out = g.graph("c1", schema)
    by_key = {(e["from"], e["from_col"]): e for e in out["edges"]}
    assert by_key[("orders", "customer_id")]["diff"] is None    # FK 重新提案，正常
    assert by_key[("orders", "campaign_id")]["diff"] == "removed"


def test_removed_not_red_when_field_deleted():
    """端点表/字段被删 -> 边消失属正常，不标红。"""
    g, schema = _conn_state()
    # 本轮 schema 里 campaigns 表没了（或 campaign_id 列没了）
    schema["tables"] = [t for t in schema["tables"] if t["name"] != "campaigns"]
    schema["columns"] = [c for c in schema["columns"] if c["table"] != "campaigns"]
    g.set_diff_base("c1", [])
    out = g.graph("c1", schema)
    by_key = {(e["from"], e["from_col"]): e for e in out["edges"]}
    assert by_key[("orders", "campaign_id")]["diff"] is None  # 字段没了，不红


def test_new_and_modified_drafts():
    """draft 边：无对应正式边 -> 绿（new）；同列对基数不同 -> 黄（modified）。"""
    g, schema = _conn_state()
    g._llm_graph_edges["c1"] = [
        {"from_table": "orders", "from_col": "region_id", "to_table": "regions", "to_col": "id",
         "kind": "naming", "cardinality": "n:1"},          # 新边
        {"from_table": "orders", "from_col": "customer_id", "to_table": "customers", "to_col": "id",
         "kind": "naming", "cardinality": "1:1"},          # 同列对，基数 n:1 -> 1:1 变化
    ]
    out = g.graph("c1", schema)
    by_key = {(d["from_table"], d["from_col"]): d for d in out["llm_draft_edges"]}
    assert by_key[("orders", "region_id")]["diff"] == "new"
    assert by_key[("orders", "customer_id")]["diff"] == "modified"


def test_pinned_edge_not_red():
    """红边"保留"(pinned) -> 不再判红。"""
    g, schema = _conn_state()
    g.set_diff_base("c1", [])
    n = g.pin_edge("c1", "orders", "campaigns", "campaign_id", "id")
    assert n == 1
    out = g.graph("c1", schema)
    by_key = {(e["from"], e["from_col"]): e for e in out["edges"]}
    assert by_key[("orders", "campaign_id")]["diff"] is None


def test_user_edge_not_red():
    """人工连线（user）永不判红。"""
    g, schema = _conn_state()
    g._graph["c1"]["edges"].append({"from": "orders", "from_col": None, "to": "regions",
                                    "to_col": None, "kind": "user", "cardinality": "n:1"})
    g.set_diff_base("c1", [])
    out = g.graph("c1", schema)
    user = [e for e in out["edges"] if e["kind"] == "user"]
    assert user and user[0]["diff"] is None


def test_diff_base_cleared_after_confirm():
    """确认全部后基线清除 -> 红边回归正常。"""
    g, schema = _conn_state()
    g.set_diff_base("c1", [])
    g.clear_diff_base("c1")
    out = g.graph("c1", schema)
    by_key = {(e["from"], e["from_col"]): e for e in out["edges"]}
    assert by_key[("orders", "campaign_id")]["diff"] is None
