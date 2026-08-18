"""AI 对话内部真实读库必须写审计（origin=ai, source=loop_internal），且读卡带结果数据。"""

from app.ai.tools import execute_tool


async def test_run_query_tool_writes_audit(app_state, conn_id):
    out = await execute_tool(app_state, "run_query", {"sql": "SELECT * FROM products LIMIT 3"}, conn_id)
    assert out.card["verdict"] == "allow"
    assert "result" in out.card                      # 卡片带结果数据（供前端展示）
    assert out.card["result"]["row_count"] == 3
    entries = app_state.audit.list(source="loop_internal")
    assert any(e["origin"] == "ai" and e["sql"].startswith("SELECT") for e in entries)


async def test_run_query_tool_result_not_in_model_view(app_state, conn_id):
    out = await execute_tool(app_state, "run_query", {"sql": "SELECT * FROM products LIMIT 3"}, conn_id)
    # 喂模型的 result 默认不含 rows（隐私红线：include_data=False）
    assert "rows" not in out.result
