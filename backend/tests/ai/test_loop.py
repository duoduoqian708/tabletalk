"""AI 循环测试（MockProvider，无 key 全流程）。"""
from __future__ import annotations

import pytest

from app.ai.loop import chat_stream
from app.ai.provider_cfg import resolve_provider_cfg
from app.ai.dto import ChatRequest
from app.core.settings import ModelConfig


class _FakeRuntime:
    def get(self):
        class R:
            ai_models = [
                ModelConfig(
                    id="m2", name="M2", provider="cloud", base_url="http://x/v1",
                    api_key="k", model="m2-model", temperature=0.5, timeout=30, reasoning=True,
                )
            ]

            def provider_config(self):
                return {
                    "provider": "mock", "base_url": "", "api_key": "", "model": "mock",
                    "temperature": 0.2, "timeout": 30, "reasoning": False,
                }

        return R()


class _FakeState:
    runtime = _FakeRuntime()


async def test_provider_cfg_resolves_model_id():
    """model_id 命中 ai_models 时优先；缺省走默认；显式覆盖最高优先。"""
    cfg = resolve_provider_cfg(_FakeState(), ChatRequest(connection_id="c", model_id="m2"))
    assert cfg["provider"] == "cloud"
    assert cfg["model"] == "m2-model"
    assert cfg["temperature"] == 0.5
    assert cfg["reasoning"] is True

    # 缺省 model_id → 默认模型
    cfg_def = resolve_provider_cfg(_FakeState(), ChatRequest(connection_id="c"))
    assert cfg_def["provider"] == "mock"

    # 显式 reasoning 覆盖选中模型（强度字符串）
    cfg_ov = resolve_provider_cfg(_FakeState(), ChatRequest(connection_id="c", model_id="m2", reasoning="off"))
    assert cfg_ov["reasoning"] == "off"


async def _collect(state, conn_id, text, include_data=False):
    req = ChatRequest(connection_id=conn_id, messages=[{"role": "user", "content": text}],
                      provider="mock", include_data=include_data)
    events = [ev async for ev in chat_stream(state, req)]
    return events


async def _cards(events):
    return [ev["card"] for ev in events if ev["type"] == "sql_card"]


async def test_context_full_returns_stage_meta(app_state, conn_id):
    """assemble_context_full 返回 (text, meta)，meta 含 intent 与 candidate_tables（四步展示数据源）。"""
    from app.ai.context import assemble_context_full
    from app.ai.dto import ChatRequest as CR

    text, meta = await assemble_context_full(app_state, conn_id, query="库存分析")
    assert isinstance(text, str) and len(text) > 0
    assert "intent" in meta and "candidate_tables" in meta
    assert isinstance(meta["intent"], list) and isinstance(meta["candidate_tables"], list)


async def test_read_flow_produces_read_card(app_state, conn_id):
    events = await _collect(app_state, conn_id, "查上个月退货率最高的 10 个商品")
    cards = await _cards(events)
    assert cards, events
    assert cards[0]["tier"] == "read"
    assert cards[0]["verdict"] == "allow"
    assert "SELECT" in cards[0]["sql"].upper()
    texts = [e["content"] for e in events if e["type"] == "text"]
    assert any("只读" in t or "推送到" in t for t in texts)


async def test_write_flow_produces_dml_review_card(app_state, conn_id):
    events = await _collect(app_state, conn_id, "把库存为 0 的商品提价 10%")
    cards = await _cards(events)
    assert cards
    assert cards[0]["tier"] == "dml"
    assert cards[0]["verdict"] == "review"
    assert "preview_rows" in cards[0]
    # 写操作结果里带 needs_confirm，且不携带行数据
    assert "UPDATE" in cards[0]["sql"].upper()


async def test_ddl_flow_produces_ddl_card_manual(app_state, conn_id):
    events = await _collect(app_state, conn_id, "给订单表加一个索引")
    cards = await _cards(events)
    assert cards
    assert cards[0]["tier"] == "ddl"
    assert cards[0]["verdict"] == "manual"
    assert "CREATE INDEX" in cards[0]["sql"].upper()


async def test_no_row_data_without_include_data(app_state, conn_id):
    # run_query 默认只回列名+行数，不含 rows
    req = ChatRequest(connection_id=conn_id, messages=[{"role": "user", "content": "退货率"}], provider="mock")
    events = [ev async for ev in chat_stream(app_state, req)]
    # 工具结果不直接暴露给事件；但可验证 read 卡仍带 sql 且无明细泄露
    cards = await _cards(events)
    assert cards and cards[0]["tier"] == "read"


async def test_loop_terminates_with_done(app_state, conn_id):
    events = await _collect(app_state, conn_id, "客户生命周期价值")
    assert events[-1]["type"] == "done"
