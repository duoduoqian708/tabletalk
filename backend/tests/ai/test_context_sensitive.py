"""敏感名单接线：assemble_context 输出不含被屏蔽表（T3 端到端）。"""

from app.ai.context import assemble_context


async def test_assemble_context_excludes_sensitive_tables(app_state, conn_id):
    # 屏蔽 big_values 表
    app_state.connections.update(conn_id, {"sensitive": ["big_values"]})
    text = await assemble_context(app_state, conn_id, query="看看数据")
    assert "big_values" not in text
    assert "products" in text   # 未屏蔽表保留
