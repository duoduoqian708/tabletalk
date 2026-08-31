"""增量构建（D1 收紧标签吸收 / D2 删表清标签+进记录 / D3 局部图谱）核心行为测试。

纯内存/mock 网关测：不依赖真实 LLM。
覆盖：
- _sync_removed_tables：删表 → 表标签绑定移除 + LLM draft 边清除 + 0 表标签清理
- _upsert_llm_edges：替换涉及目标的旧边、保留其它、去重
- annotate_domain(mode="incremental")：只 assign 目标表、不清已有标签、新域 draft
- annotate_graph(mode="incremental")：mock 走 FK 元数据边（局部子图）
"""
from __future__ import annotations

from app.knowledge.store import KnowledgeBase


def _mk() -> KnowledgeBase:
    return KnowledgeBase("/tmp/tt-kb-incr-test")


def _mk_ctx():
    """构造一个已完成构建的 KnowledgeBase + 一组标签/边，供增量场景复现。"""
    kb = _mk()
    # 已确认标签体系（模拟人工确认后的库）
    kb.create_tag("c1", "订单", "订单域")
    kb.create_tag("c1", "客户", "客户域")
    kb.assign_table_tags("c1", "orders", ["订单"])
    kb.assign_table_tags("c1", "customers", ["客户"])
    # LLM draft 边（模拟之前构建遗留）
    kb._llm_graph_edges["c1"] = [
        {"from_table": "orders", "from_col": "customer_id", "to_table": "customers",
         "to_col": "id", "cardinality": "n:1", "reason": "订单归客户",
         "confidence": "high", "source": "llm_global"},
        {"from_table": "returns", "from_col": "order_item_id", "to_table": "order_items",
         "to_col": "id", "cardinality": "n:1", "reason": "退货归明细",
         "confidence": "high", "source": "llm_global"},
    ]
    return kb


# ---------- D2：删表清理 ----------


def test_sync_removed_tables_cleans_bindings_edges_tags():
    kb = _mk_ctx()
    # 删除 orders → 该表绑定移除 + 涉及它的 LLM 边清除；returns 边保留
    cleared = kb._sync_removed_tables("c1", {"orders"})
    # orders 标签绑定被移除 → 订单标签 0 表 → 清理（D2）
    assert "订单" in cleared
    assert "orders" not in kb._table_tags.get("c1", {})
    edges = kb._llm_graph_edges["c1"]
    assert all(e["from_table"] != "orders" and e["to_table"] != "orders" for e in edges)
    assert any(e["from_table"] == "returns" for e in edges)  # 无关边保留
    assert "订单" not in kb._tags.get("c1", {})  # 0 表标签已清
    assert "客户" in kb._tags.get("c1", {})      # 客户仍绑 customers，保留


def test_sync_removed_tables_empty_noop():
    kb = _mk_ctx()
    assert kb._sync_removed_tables("c1", set()) == []


# ---------- D3：局部 LLM 边替换 ----------


def test_upsert_llm_edges_replaces_target_keeps_others():
    kb = _mk_ctx()
    new_edges = [
        {"from_table": "orders", "from_col": "customer_id", "to_table": "customers",
         "to_col": "id", "cardinality": "n:1", "reason": "新", "confidence": "high"},
        {"from_table": "orders", "from_col": "address_id", "to_table": "addresses",
         "to_col": "id", "cardinality": "n:1", "reason": "新地址", "confidence": "high"},
    ]
    kb._upsert_llm_edges("c1", {"orders"}, new_edges)
    edges = kb._llm_graph_edges["c1"]
    # returns 无关边保留
    assert any(e["from_table"] == "returns" for e in edges)
    # orders 旧边被替换为新边，且不重复
    orders_edges = [e for e in edges if e["from_table"] == "orders" or e["to_table"] == "orders"]
    assert len(orders_edges) == 2
    assert any(e.get("to_table") == "addresses" for e in orders_edges)
    assert all(e.get("reason") == "新" or e.get("reason") == "新地址" for e in orders_edges)


def test_upsert_llm_edges_reproposes_after_reject():
    """拒绝过的边在增量补边时照常重新提案（无墓碑，去重只按当前 draft 列表）。"""
    kb = _mk_ctx()
    new_edges = [
        {"from_table": "orders", "from_col": "product_id", "to_table": "products",
         "to_col": "id", "cardinality": "n:1", "reason": "重新提案", "confidence": "high"},
    ]
    kb._upsert_llm_edges("c1", {"orders"}, new_edges)
    edges = kb._llm_graph_edges["c1"]
    assert any(e["to_table"] == "products" for e in edges)


# ---------- D1：增量标签吸收（mock） ----------


def test_annotate_domain_incremental_mock_only_target(app_state, monkeypatch):
    """mock 增量吸收：只 assign 目标表、不清已有标签、新域为 draft。"""
    import asyncio

    from app.knowledge import annotator as ann

    kb = app_state.knowledge
    kb.create_tag("c1", "订单", "订单域")          # 已确认候选
    kb.confirm_tag("c1", "订单")
    kb.assign_table_tags("c1", "orders", ["订单"])
    # mock 模式（is_effective_mock 命中）
    monkeypatch.setattr(ann.gw, "is_effective_mock", lambda cfg: True)
    schema = {
        "tables": [{"name": "orders", "kind": "table", "comment": "", "column_count": 2},
                   {"name": "new_orders", "kind": "table", "comment": "", "column_count": 1}],
        "columns": [
            {"table": "orders", "name": "id", "type": "int", "pk": True, "fk": False, "comment": ""},
            {"table": "orders", "name": "customer_id", "type": "int", "pk": False, "fk": True, "comment": ""},
            {"table": "new_orders", "name": "id", "type": "int", "pk": True, "fk": False, "comment": ""},
        ],
        "foreign_keys": [],
    }
    res = asyncio.run(ann.annotate_domain(
        app_state, "c1", schema, mode="incremental", target_tables=["new_orders"],
    ))
    # 只处理目标表
    assert res["tables"] == 1
    # 已有标签不动；新域（如有）为 draft
    lib = {t["name"]: t for t in kb.tags("c1")["library"]}
    assert lib["订单"]["status"] == "confirmed"   # 已确认不动
    # new_orders 已被绑到某域（mock 会命中 order→订单，或兜底）
    assert "new_orders" in kb._table_tags.get("c1", {})
    # orders 的绑定没有被破坏（still 订单）
    assert kb._table_tags["c1"]["orders"] == ["订单"]


# ---------- D3：增量图谱（mock FK 元数据边） ----------


def test_annotate_graph_incremental_mock_fk_edges(app_state, monkeypatch):
    """mock 增量图谱：局部 FK 子图产边（不依赖 LLM）。"""
    import asyncio

    from app.knowledge import annotator as ann

    monkeypatch.setattr(ann.gw, "is_effective_mock", lambda cfg: True)
    schema = {
        "tables": [{"name": "orders", "kind": "table", "comment": "", "column_count": 2},
                   {"name": "new_invoices", "kind": "table", "comment": "", "column_count": 2}],
        "columns": [
            {"table": "orders", "name": "id", "type": "int", "pk": True, "fk": False, "comment": ""},
            {"table": "new_invoices", "name": "order_id", "type": "int", "pk": False, "fk": True, "comment": ""},
        ],
        "foreign_keys": [
            {"table": "new_invoices", "column": "order_id", "ref_table": "orders", "ref_column": "id"},
        ],
    }
    edges = asyncio.run(ann.annotate_graph(
        app_state, "c1", schema, mode="incremental", target_tables=["new_invoices"],
    ))
    # mock → FK 元数据边，且只含目标表相关
    assert edges, "应有 FK 边"
    assert all(e["from_table"] == "new_invoices" or e["to_table"] == "new_invoices" for e in edges)