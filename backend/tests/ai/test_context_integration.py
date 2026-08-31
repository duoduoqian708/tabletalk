"""T9 消费流程整合测试：路径串 / 会话变量 / 表级过滤器 / 语义检索限定范围 / meta。"""
from __future__ import annotations

from app.ai.context import assemble_context_full
from app.knowledge.filters import TableFilter


async def _build(app_state, conn_id) -> None:
    from app.core.schema import get_schema
    await app_state.knowledge.build(conn_id, await get_schema(app_state, conn_id),
                                    enable_ai_annotation=False)
    # 2026-08-31 修订：边默认 draft，路径串按已确认边生成 → 先确认
    app_state.knowledge.confirm_graph_edges(conn_id)


async def test_path_strings_in_prompt(app_state, conn_id):
    """demo 库有 FK → prompt 含路径串 + meta.paths 非空。"""
    await _build(app_state, conn_id)
    text, meta = await assemble_context_full(app_state, conn_id, query="returns 表里都有哪些退货记录")
    assert "关联路径" in text
    assert isinstance(meta.get("paths"), list) and len(meta["paths"]) > 0
    assert any(". = " in p or "=" in p for p in meta["paths"])


async def test_session_vars_in_prompt(app_state, conn_id):
    """prompt 含会话变量清单（名称+含义，不含真实值）。"""
    await _build(app_state, conn_id)
    text, _ = await assemble_context_full(app_state, conn_id, query="returns 表里都有哪些退货记录")
    assert ":current_tenant" in text
    assert "当前租户ID" in text
    assert "可用会话变量" in text
    assert "7" not in text.split("可用会话变量")[1][:200]


async def test_filters_in_prompt(app_state, conn_id):
    """候选表有 confirmed 过滤器 → prompt 出现过滤谓词。"""
    await _build(app_state, conn_id)
    fs = app_state.knowledge.filter_store
    fs._filters[conn_id] = {
        "orders": TableFilter(table="orders", predicate="is_deleted = 0", status="confirmed"),
    }
    text, meta = await assemble_context_full(app_state, conn_id, query="returns 表里都有哪些退货记录")
    assert meta.get("filters", {}).get("orders") == ["is_deleted = 0"]
    assert "is_deleted = 0" in text


async def test_retrieval_scoped_to_candidates(app_state, conn_id):
    """语义检索限定在候选表内：白名单外同名表不再被召回。"""
    await _build(app_state, conn_id)
    # 无白名单：退货相关召回 returns
    text_all = await app_state.knowledge.to_context(conn_id, query="returns 退货", k=5)
    assert "returns" in text_all
    # 白名单 {orders}：returns 被过滤
    text_scoped = await app_state.knowledge.to_context(
        conn_id, query="returns 退货", k=5, table_whitelist={"orders"})
    assert "returns" not in text_scoped


async def test_skip_retrieval_no_paths_vars(app_state, conn_id):
    """结构问答（skip_retrieval=True）→ 无路径串/会话变量/过滤器。"""
    await _build(app_state, conn_id)
    text, meta = await assemble_context_full(app_state, conn_id, query="有哪些表",
                                             skip_retrieval=True)
    assert meta.get("paths") == []
    assert ":current_tenant" not in text
    assert "【关联路径】" not in text


async def test_meta_fields(app_state, conn_id):
    """meta 含 paths/filters 字段（前端四步展示）。"""
    await _build(app_state, conn_id)
    _, meta = await assemble_context_full(app_state, conn_id, query="returns 表里都有哪些退货记录")
    assert "paths" in meta and "filters" in meta


async def test_sensitive_table_codified(app_state, conn_id):
    """B3 不变式：路径串/过滤器中的敏感表名代号化（出网无明文，meta 同步代号）。"""
    from app.core.schema import get_schema
    kb = app_state.knowledge
    await kb.build(conn_id, await get_schema(app_state, conn_id), enable_ai_annotation=False)
    kb.confirm_graph_edges(conn_id)
    # orders 打 confirmed sensitive 标签 → B3 敏感表
    kb.create_tag(conn_id, "sensitive", description="", color="")
    kb.assign_table_tags(conn_id, "orders", ["sensitive"])
    kb.confirm_tag(conn_id, "sensitive")
    # orders 加一条表级过滤器，验证过滤器段也代号化
    fs = kb.filter_store
    fs._filters[conn_id] = {
        "orders": TableFilter(table="orders", predicate="is_deleted = 0", status="confirmed"),
    }

    # returns → order_items → orders 链路，保证敏感表 orders 进入候选路径
    text, meta = await assemble_context_full(app_state, conn_id, query="returns 表里都有哪些退货记录")
    # 前置：候选表确实包含敏感表 orders（种子链路可达）
    assert meta["candidate_tables"], "应有候选表"
    # 1. meta.paths 非空，且其中敏感表被代号化（无明文 orders）
    assert meta["paths"], "应有路径串（returns 有 FK 链路）"
    assert all("orders" not in p for p in meta["paths"]), "路径串含明文敏感表名"
    # 2. prompt 的【关联路径】段无明文 orders
    if "【关联路径】" in text:
        seg = text.split("【关联路径】")[1].split("【")[0]
        assert "orders" not in seg, "关联路径段含明文敏感表名"
    # 3. 过滤器段 + meta.filters：key 不是明文 orders
    if meta.get("filters"):
        assert "orders" not in meta["filters"], "meta.filters 含明文敏感表名"
        if "【表级过滤】" in text:
            seg = text.split("【表级过滤】")[1].split("【")[0]
            assert "orders" not in seg, "表级过滤段含明文敏感表名"


async def test_fewshot_recalled_in_prompt(app_state, conn_id):
    """T10：历史成功问答召回 → 【相似问答】段 + meta.fewshot。"""
    await _build(app_state, conn_id)
    kb = app_state.knowledge
    kb.fewshot_store.add(
        conn_id, "用户订单查询",
        "SELECT * FROM orders JOIN users ON orders.user_id = users.id",
        ["orders.user_id = users.id"],
    )
    text, meta = await assemble_context_full(app_state, conn_id, query="用户订单查询")
    assert meta.get("fewshot", 0) >= 1
    assert "相似问答" in text
    assert "orders.user_id = users.id" in text


async def test_fewshot_sensitive_codified(app_state, conn_id):
    """B3：敏感表（confirmed sensitive 标签）的 few-shot 问答段代号化出网。"""
    await _build(app_state, conn_id)
    kb = app_state.knowledge
    kb.fewshot_store.add(conn_id, "订单对账", "SELECT * FROM orders", [])
    kb.semantic_store.assign_table_tags(conn_id, "orders", ["sensitive"])
    kb.semantic_store.confirm_tag(conn_id, "sensitive")
    text, meta = await assemble_context_full(app_state, conn_id, query="订单对账")
    assert meta.get("fewshot", 0) >= 1
    assert "相似问答" in text
    assert "orders" not in text  # 敏感表名全程无明文


async def test_skip_retrieval_no_kb_recall(app_state, conn_id):
    """R13：skip_retrieval=True 时 KB 向量召回零调用（WS2 真正短路）。"""
    await _build(app_state, conn_id)
    calls: list[int] = []
    kb = app_state.knowledge
    orig = kb.to_context

    async def _spy(*a, **kw):
        calls.append(1)
        return await orig(*a, **kw)

    kb.to_context = _spy
    try:
        text, meta = await assemble_context_full(app_state, conn_id, query="有哪些表",
                                                 skip_retrieval=True)
    finally:
        kb.to_context = orig
    assert calls == []  # 结构问答零向量召回
    assert meta.get("kb_docs", 0) == 0
    assert "相似问答" not in text


async def test_concepts_in_prompt(app_state, conn_id):
    """B2/T9：候选表内 confirmed 概念 → 【概念字典】段（限定候选表、B3 代号化）。"""
    from app.core.schema import get_schema
    from app.knowledge.semantic.concepts import Concept

    await app_state.knowledge.build(conn_id, await get_schema(app_state, conn_id),
                                    enable_ai_annotation=False)
    app_state.knowledge.confirm_graph_edges(conn_id)
    kb = app_state.knowledge
    # demo 库含 orders.status → confirmed 概念
    kb.concept_store.upsert(conn_id, Concept(
        name="订单状态",
        canonical_enum=[{"code": "P", "label": "待付款"}, {"code": "S", "label": "已发货"}],
        members=[{"table": "orders", "column": "status", "mapping": "code"}],
        status="confirmed",
    ))
    text, meta = await assemble_context_full(app_state, conn_id, query="returns 表里都有哪些退货记录")
    assert "概念字典" in text
    assert "订单状态" in text
    assert "待付款" in text


async def test_concepts_scoped_to_candidates(app_state, conn_id):
    """概念限定候选表：成员表不在候选 → 概念不进 prompt。"""
    from app.core.schema import get_schema
    from app.knowledge.semantic.concepts import Concept

    await app_state.knowledge.build(conn_id, await get_schema(app_state, conn_id),
                                    enable_ai_annotation=False)
    app_state.knowledge.confirm_graph_edges(conn_id)
    kb = app_state.knowledge
    # 幽灵表概念（不在任何 schema 中）→ 即使 confirmed 也不进 prompt
    kb.concept_store.upsert(conn_id, Concept(
        name="幽灵维度",
        canonical_enum=[{"code": "X", "label": "幽灵"}],
        members=[{"table": "ghost_table", "column": "id", "mapping": "identity"}],
        status="confirmed",
    ))
    text, _ = await assemble_context_full(app_state, conn_id, query="returns 表里都有哪些退货记录")
    assert "幽灵维度" not in text


async def test_concepts_sensitive_codified(app_state, conn_id):
    """B3：confirmed sensitive 表的概念段代号化出网。"""
    from app.core.schema import get_schema
    from app.knowledge.semantic.concepts import Concept

    await app_state.knowledge.build(conn_id, await get_schema(app_state, conn_id),
                                    enable_ai_annotation=False)
    app_state.knowledge.confirm_graph_edges(conn_id)
    kb = app_state.knowledge
    kb.concept_store.upsert(conn_id, Concept(
        name="订单状态",
        canonical_enum=[{"code": "P", "label": "待付款"}],
        members=[{"table": "orders", "column": "status", "mapping": "code"}],
        status="confirmed",
    ))
    kb.create_tag(conn_id, "sensitive", description="", color="")
    kb.assign_table_tags(conn_id, "orders", ["sensitive"])
    kb.confirm_tag(conn_id, "sensitive")
    text, meta = await assemble_context_full(app_state, conn_id, query="returns 表里都有哪些退货记录")
    if "概念字典" in text:
        seg = text.split("概念字典")[1].split("【")[0]
        assert "orders" not in seg, "概念段含明文敏感表名"
        assert "订单状态" in seg  # 概念名本身保留（不涉表名）
