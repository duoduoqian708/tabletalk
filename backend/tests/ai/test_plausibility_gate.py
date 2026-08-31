"""R15/R1 图校验器（plausibility gate）测试：AI 查询的 JOIN 必须命中知识库图。

对齐设计 §10⑤：LLM 生成的 join 不在图里 → 打回重写（非安全 BLOCK）。
"""
from __future__ import annotations

import pytest

from app.ai.tools.registry import execute_tool


@pytest.fixture(autouse=True)
def _reset_fail_counter():
    """P3：隔离模块级降级计数器（测试不共享全局状态）。"""
    import app.ai.tools.sql as sql_mod
    sql_mod._plausibility_fail_count.clear()
    yield
    sql_mod._plausibility_fail_count.clear()


async def _ensure_kb_built(app_state, conn_id) -> None:
    from app.core.schema import get_schema
    if conn_id not in app_state.knowledge._auto:
        await app_state.knowledge.build(conn_id, await get_schema(app_state, conn_id),
                                        enable_ai_annotation=False)
    # 2026-08-31 修订：边默认 draft，图校验按已确认边生效 → 先确认
    app_state.knowledge.confirm_graph_edges(conn_id)


def _enable_real_gate(app_state) -> None:
    """图校验在 mock 下跳过（演示降级）；测试切到非 mock 以走真实校验路径。"""
    app_state.runtime.update({"ai_provider": "cloud", "ai_api_key": "test-key"})


async def _run(app_state, conn_id, sql: str) -> dict:
    out = await execute_tool(app_state, "run_query", {"sql": sql}, conn_id)
    return out.result


async def test_gate_in_graph_allowed(app_state, conn_id):
    """图内 join（FK 声明：orders.customer_id → customers.id）→ 正常放行执行。"""
    _enable_real_gate(app_state)
    await _ensure_kb_built(app_state, conn_id)
    r = await _run(app_state, conn_id,
                   "SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id LIMIT 1")
    assert r.get("ok") is True, r


async def test_gate_out_of_graph_rejected(app_state, conn_id):
    """图外 join（orders.campaign_id → customers.id 跨表错连）→ 打回重写（ok=False + hint），不执行。"""
    _enable_real_gate(app_state)
    await _ensure_kb_built(app_state, conn_id)
    r = await _run(app_state, conn_id,
                   "SELECT * FROM orders JOIN customers ON orders.campaign_id = customers.id LIMIT 1")
    assert r.get("ok") is False, r
    assert "hint" in r and "图" in r.get("hint", ""), r


async def test_gate_aliased_join_allowed(app_state, conn_id):
    """别名 join（orders o JOIN customers c ON o.customer_id = c.id）→ 放行。"""
    _enable_real_gate(app_state)
    await _ensure_kb_built(app_state, conn_id)
    r = await _run(app_state, conn_id,
                   "SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id LIMIT 1")
    assert r.get("ok") is True, r


async def test_gate_single_table_no_join_skipped(app_state, conn_id):
    """无 JOIN 单表查询 → 不校验、直接执行。"""
    _enable_real_gate(app_state)
    await _ensure_kb_built(app_state, conn_id)
    r = await _run(app_state, conn_id, "SELECT * FROM orders LIMIT 1")
    assert r.get("ok") is True, r


async def test_gate_degrades_after_repeated_failures(app_state, conn_id):
    """同 turn 连续失败 >2 次 → 降级放行（防死循环）。"""
    _enable_real_gate(app_state)
    await _ensure_kb_built(app_state, conn_id)
    sql = "SELECT * FROM orders JOIN customers ON orders.campaign_id = customers.id LIMIT 1"
    from app.knowledge.graph.sql_joins import extract_join_pairs
    pairs = extract_join_pairs(sql)
    assert len(pairs) == 1  # 解析本身正常
    # 图里确实没有该边（打回条件成立）
    assert app_state.knowledge.validate_join(conn_id, "orders", "campaign_id", "customers", "id") is False
    # 连续失败：第 1 次打回、第 2 次打回、第 3 次起降级放行
    r1 = await _run(app_state, conn_id, sql)
    assert r1.get("ok") is False
    r2 = await _run(app_state, conn_id, sql)
    assert r2.get("ok") is False
    r3 = await _run(app_state, conn_id, sql)
    assert r3.get("ok") is True, r3  # 降级放行


async def test_gate_mock_out_of_graph_validate(app_state, conn_id):
    """前置确认：图里确实无该边（打回条件成立），mock 演示降级路径可放行。"""
    await _ensure_kb_built(app_state, conn_id)
    ok = app_state.knowledge.validate_join(conn_id, "orders", "shipped_by", "users", "id")
    assert ok is False


async def test_gate_success_resets_failure_count(app_state, conn_id):
    """P1-4：校验成功即清零——失败 1 次 → 成功 1 次 → 再失败仍打回（不再跨轮累计）。"""
    _enable_real_gate(app_state)
    await _ensure_kb_built(app_state, conn_id)
    bad = "SELECT * FROM orders JOIN customers ON orders.campaign_id = customers.id LIMIT 1"
    good = "SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id LIMIT 1"
    r1 = await _run(app_state, conn_id, bad)
    assert r1.get("ok") is False  # 失败 1 次
    r2 = await _run(app_state, conn_id, good)
    assert r2.get("ok") is True   # 成功 → 计数器清零
    r3 = await _run(app_state, conn_id, bad)
    assert r3.get("ok") is False, r3  # 仍严格打回（若未清零，此轮会降级放行）


async def test_gate_window_expiry_resets(app_state, conn_id, monkeypatch):
    """P1-4：60s 时间窗外计数器归零（"同 turn 连续失败"语义：跨轮不累计）。"""
    import time
    import app.ai.tools.sql as sql_mod

    _enable_real_gate(app_state)
    await _ensure_kb_built(app_state, conn_id)
    bad = "SELECT * FROM orders JOIN customers ON orders.campaign_id = customers.id LIMIT 1"
    r1 = await _run(app_state, conn_id, bad)
    assert r1.get("ok") is False  # 计数 1
    # 把计数挪到 61s 前（模拟跨轮）：应归零，第 2 次失败仍打回而非降级放行
    _c, _ts = sql_mod._plausibility_fail_count[conn_id]
    sql_mod._plausibility_fail_count[conn_id] = (_c, time.time() - 61)
    assert sql_mod._plausibility_fails(conn_id) == 0
    r2 = await _run(app_state, conn_id, bad)
    assert r2.get("ok") is False, r2  # 窗口重置，仍打回
    r3 = await _run(app_state, conn_id, bad)
    assert r3.get("ok") is False, r3  # 重新累计到 2，仍打回


# ---------- R3：空结果反馈（§10⑥） ----------

async def test_empty_result_has_hint(app_state, conn_id):
    """查询成功但 0 行 → card 附 empty_hint（提示模型自检，不阻断）。"""
    _enable_real_gate(app_state)
    await _ensure_kb_built(app_state, conn_id)
    out = await execute_tool(app_state, "run_query",
                             {"sql": "SELECT * FROM orders WHERE id = -999"}, conn_id)
    assert out.result.get("ok") is True, out.result
    assert out.result.get("row_count") == 0
    assert out.card is not None and out.card.get("empty_hint"), out.card


async def test_non_empty_result_no_hint(app_state, conn_id):
    """查询有行 → 无 empty_hint。"""
    _enable_real_gate(app_state)
    await _ensure_kb_built(app_state, conn_id)
    out = await execute_tool(app_state, "run_query",
                             {"sql": "SELECT * FROM orders LIMIT 1"}, conn_id)
    assert out.result.get("ok") is True
    assert out.result.get("row_count", 0) > 0
    assert not (out.card or {}).get("empty_hint")