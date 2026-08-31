"""敏感名单接线：assemble_context 输出不含被屏蔽表（T3 端到端）。"""

from app.ai.context import assemble_context, assemble_context_full


async def test_assemble_context_excludes_sensitive_tables(app_state, conn_id):
    # 屏蔽 big_values 表
    app_state.connections.update(conn_id, {"sensitive": ["big_values"]})
    text = await assemble_context(app_state, conn_id, query="看看数据")
    assert "big_values" not in text
    assert "products" in text   # 未屏蔽表保留


async def test_assemble_context_keeps_non_sensitive_paths(app_state, conn_id):
    """P1-7：候选路径不含敏感表时整条保留（此前被空内层推导丢弃）。"""
    from app.ai.context import assemble_context, assemble_context_full

    app_state.connections.update(conn_id, {"sensitive": ["big_values"]})
    try:
        kb = app_state.knowledge
        if conn_id not in kb._auto:
            from app.core.schema import get_schema
            await kb.build(conn_id, await get_schema(app_state, conn_id),
                           enable_ai_annotation=False)
        kb.confirm_graph_edges(conn_id)
    except Exception:
        pass  # 构建失败则跳过路径断言（环境依赖）
    # followup_tables 强制种子 → 候选子图 + 路径串段落（不依赖向量召回）
    text, _ = await assemble_context_full(app_state, conn_id, query="关联",
                                          followup_tables=["orders", "customers"])
    assert "【关联路径】" in text, "路径串段落应存在（无论是否含敏感表）"
    # 敏感表名仍不可见，但段落本身不被丢弃
    assert "big_values" not in text
