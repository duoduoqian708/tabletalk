"""问题库（续流/沉淀）主流程：保存限只读、命中出确认卡、表名纠错注入可用表。

来源：原 ws5 T5.2/T5.4/T5.5（合并自 test_ws5.py，删 T5.1/T5.3/T5.6 细节断言，走查覆盖）。
"""
import pytest

from app.ai.dto import ChatRequest
from app.ai.loop import stream
from app.ai.tools.sql import _run_query


async def test_run_query_unknown_table_injects_available(app_state, conn_id):
    res = await _run_query(app_state, {"sql": "SELECT * FROM not_exist_table_xyz"}, conn_id, include_data=False)
    assert res.result.get("available_tables"), "不存在表应返回可用表清单（bounded 纠错）"


async def test_question_hit_emits_confirm_card_not_execute(app_state, conn_id):
    """命中已沉淀问题 → question_library 确认卡（needs_confirm、不附结果、不直接执行）。"""
    store = app_state.questions
    for e in store.list(conn_id):
        store.delete(conn_id, e["id"])
    q = "查一下订单总数确认卡测试"
    sql = "SELECT COUNT(*) FROM orders"
    entry = store.save(conn_id, q, sql, tables=["orders"])
    req = ChatRequest(connection_id=conn_id, messages=[{"role": "user", "content": q}], provider="mock")
    events = [ev async for ev in stream(app_state, req)]
    cards = [ev for ev in events if ev["type"] == "sql_card"]
    assert len(cards) == 1 and cards[0]["card"].get("question_library") is True
    assert cards[0]["card"].get("needs_confirm") is True
    assert cards[0]["card"].get("sql") == sql
    store.delete(conn_id, entry["id"])


async def test_question_save_rejects_non_readonly(app_state, conn_id):
    store = app_state.questions
    e1 = store.save(conn_id, "查订单", "SELECT * FROM orders LIMIT 10")
    assert e1["sql"].startswith("SELECT")
    store.delete(conn_id, e1["id"])
    with pytest.raises(ValueError, match="仅收只读"):
        store.save(conn_id, "删订单", "DELETE FROM orders WHERE id=1")
