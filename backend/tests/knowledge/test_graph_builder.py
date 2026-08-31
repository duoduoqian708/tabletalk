"""建边管线测试：FK / 命名推断 / 统一 draft 产出（值重叠与判别器检测已删，2026-08-31）。"""
from __future__ import annotations

from app.knowledge.graph.builder import (
    build_draft_edges,
    build_fk_edges,
    build_naming_edges,
    build_query_log_edges,
)


def _schema(tables, columns, fks) -> dict:
    return {"tables": tables, "columns": columns, "foreign_keys": fks}


def _col(table, name, ctype="int", pk=False, fk=False) -> dict:
    return {"table": table, "name": name, "type": ctype, "nullable": True,
            "pk": pk, "fk": fk, "default": None, "comment": ""}


# ---------- FK ----------

def test_fk_edges_basic():
    schema = _schema(
        [{"name": "orders", "kind": "table", "comment": "", "column_count": 2},
         {"name": "customers", "kind": "table", "comment": "", "column_count": 1}],
        [_col("orders", "id", pk=True), _col("orders", "customer_id", fk=True),
         _col("customers", "id", pk=True)],
        [{"table": "orders", "column": "customer_id", "ref_table": "customers", "ref_column": "id"}],
    )
    edges = build_fk_edges(schema)
    assert len(edges) == 1
    e = edges[0]
    assert e.confidence == 1.0
    assert e.provenance == "declared_fk"
    assert e.cols == [("customer_id", "id")]


def test_fk_parallel_edges_not_merged():
    """同表对两条平行 FK → 两条边（不合并）。"""
    schema = _schema(
        [{"name": "orders", "kind": "table", "comment": "", "column_count": 3},
         {"name": "users", "kind": "table", "comment": "", "column_count": 1}],
        [_col("orders", "id", pk=True), _col("orders", "user_id", fk=True),
         _col("orders", "shipped_by", fk=True), _col("users", "id", pk=True)],
        [{"table": "orders", "column": "user_id", "ref_table": "users", "ref_column": "id"},
         {"table": "orders", "column": "shipped_by", "ref_table": "users", "ref_column": "id"}],
    )
    edges = build_fk_edges(schema)
    assert len(edges) == 2


def test_fk_composite():
    """复合 FK（同约束多列对，constraint_id 相同）→ 合并为 1 条边，cols 长度 2（T3 §1 复合键）。"""
    schema = _schema(
        [{"name": "order_lines", "kind": "table", "comment": "", "column_count": 3},
         {"name": "orders", "kind": "table", "comment": "", "column_count": 2}],
        [_col("order_lines", "order_id", fk=True), _col("order_lines", "line_no", fk=True),
         _col("orders", "id", pk=True), _col("orders", "line_no", pk=True)],
        [
            {"table": "order_lines", "column": "order_id", "ref_table": "orders",
             "ref_column": "id", "constraint_id": 7},
            {"table": "order_lines", "column": "line_no", "ref_table": "orders",
             "ref_column": "line_no", "constraint_id": 7},
        ],
    )
    edges = build_fk_edges(schema)
    assert len(edges) == 1
    e = edges[0]
    assert set(e.cols) == {("order_id", "id"), ("line_no", "line_no")}


def test_fk_composite_distinct_constraints_not_merged():
    """同表对两个不同复合约束（constraint_id 不同）→ 不合并（两条边）。"""
    schema = _schema(
        [{"name": "order_lines", "kind": "table", "comment": "", "column_count": 4},
         {"name": "orders", "kind": "table", "comment": "", "column_count": 2}],
        [_col("order_lines", "order_id", fk=True), _col("order_lines", "line_no", fk=True),
         _col("orders", "id", pk=True), _col("orders", "line_no", pk=True)],
        [
            {"table": "order_lines", "column": "order_id", "ref_table": "orders",
             "ref_column": "id", "constraint_id": 1},
            {"table": "order_lines", "column": "line_no", "ref_table": "orders",
             "ref_column": "line_no", "constraint_id": 2},
        ],
    )
    edges = build_fk_edges(schema)
    assert len(edges) == 2


# ---------- 命名推断 ----------

def test_naming_user_id():
    schema = _schema(
        [{"name": "orders", "kind": "table", "comment": "", "column_count": 2},
         {"name": "users", "kind": "table", "comment": "", "column_count": 1}],
        [_col("orders", "id", pk=True), _col("orders", "user_id"), _col("users", "id", pk=True)],
        [],
    )
    edges = build_naming_edges(schema)
    assert len(edges) == 1
    e = edges[0]
    assert e.source_table == "orders" and e.target_table == "users"
    assert e.confidence == 0.6
    assert e.provenance == "naming_inference"
    assert e.cols == [("user_id", "id")]


def test_naming_type_mismatch():
    schema = _schema(
        [{"name": "orders", "kind": "table", "comment": "", "column_count": 2},
         {"name": "users", "kind": "table", "comment": "", "column_count": 1}],
        [_col("orders", "id", pk=True), _col("orders", "user_id", ctype="int"),
         _col("users", "id", ctype="varchar", pk=True)],
        [],
    )
    assert build_naming_edges(schema) == []


def test_naming_self_loop_skipped():
    schema = _schema(
        [{"name": "orders", "kind": "table", "comment": "", "column_count": 2}],
        [_col("orders", "id", pk=True), _col("orders", "orders_id")],
        [],
    )
    assert build_naming_edges(schema) == []


def test_naming_plural_variant():
    """category_id → categories 表（复数变体）。"""
    schema = _schema(
        [{"name": "products", "kind": "table", "comment": "", "column_count": 2},
         {"name": "categories", "kind": "table", "comment": "", "column_count": 1}],
        [_col("products", "id", pk=True), _col("products", "category_id"),
         _col("categories", "id", pk=True)],
        [],
    )
    edges = build_naming_edges(schema)
    assert len(edges) == 1
    assert edges[0].target_table == "categories"


# ---------- 查询日志占位 ----------

def test_query_log_stub():
    assert build_query_log_edges([{"sql": "SELECT 1"}]) == []


# ---------- 统一 draft 产出（R5 重写：一切边经人工确认才生效） ----------

def test_build_draft_edges_fk_naming_dedup():
    """FK 与命名命中同一列对：FK 胜（confidence 1.0 > 0.6）；产出为 draft dict。"""
    schema = _schema(
        [{"name": "orders", "kind": "table", "comment": "", "column_count": 2},
         {"name": "customers", "kind": "table", "comment": "", "column_count": 1},
         {"name": "regions", "kind": "table", "comment": "", "column_count": 1}],
        [_col("orders", "id", pk=True), _col("orders", "customer_id"),
         _col("orders", "region_id"),
         _col("customers", "id", pk=True),
         _col("regions", "id", pk=True)],
        [{"table": "orders", "column": "customer_id", "ref_table": "customers", "ref_column": "id"}],
    )
    drafts = build_draft_edges(schema)
    # FK 列对（customer_id -> customers.id）只出一条，kind=fk
    cust = [d for d in drafts if d["from_table"] == "orders" and d["to_table"] == "customers"]
    assert len(cust) == 1 and cust[0]["kind"] == "fk" and cust[0]["confidence"] == 1.0
    # 命名推断边（orders.region_id -> regions.id）为 draft，kind=naming
    naming = [d for d in drafts if d["kind"] == "naming"]
    assert len(naming) == 1 and naming[0]["to_table"] == "regions"
    # 统一 draft 格式
    d = drafts[0]
    assert {"from_table", "from_col", "to_table", "to_col", "kind",
            "cardinality", "reason", "guard", "confidence", "provenance", "cols"} <= set(d)


def test_build_draft_edges_deterministic():
    """确定性：同 schema 重建产出完全一致的 draft 边（无跨版本记忆）。"""
    schema = _schema(
        [{"name": "orders", "kind": "table", "comment": "", "column_count": 1},
         {"name": "customers", "kind": "table", "comment": "", "column_count": 1}],
        [_col("orders", "customer_id"), _col("customers", "id", pk=True)],
        [],
    )
    assert build_draft_edges(schema) == build_draft_edges(schema)
