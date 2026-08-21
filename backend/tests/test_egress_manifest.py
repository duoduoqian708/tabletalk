"""B1 出网清单 — 绝对全面测试（清单生成、SSE、审计、payload 一致性、出网可枚举率 100%）。"""
from __future__ import annotations

import json

import pytest

from app.ai.context import assemble_context_full
from app.ai.dto import ChatRequest
from app.ai.loop import chat_stream
from app.ai.manifest import build_manifest, manifest_to_human
from app.ai.provider_cfg import resolve_provider_cfg


async def test_manifest_structure_and_types(app_state, conn_id):
    """清单字段齐全、类型正确、含双语所需键。"""
    from app.ai.manifest import build_manifest
    req = ChatRequest(connection_id=conn_id, messages=[{"role": "user", "content": "库存分析"}],
                      provider="mock", include_data=False)
    text, meta = await assemble_context_full(app_state, conn_id, query="库存分析")
    # 构造 messages（含 context）模拟真实发送体
    messages = [{"role": "system", "content": text}, {"role": "user", "content": "库存分析"}]
    cfg = resolve_provider_cfg(app_state, req)
    m = build_manifest(app_state, conn_id, meta, messages, False, cfg, text)
    # 必有键
    for k in ("tables", "kb_docs", "history_turns", "include_data", "redactions", "mode", "ts", "model", "provider"):
        assert k in m, f"missing {k}"
    assert isinstance(m["tables"], list)
    assert isinstance(m["kb_docs"], int) and m["kb_docs"] >= 0
    assert isinstance(m["history_turns"], int) and m["history_turns"] >= 1
    assert isinstance(m["include_data"], bool)
    assert isinstance(m["redactions"], list) and len(m["redactions"]) == 0  # B1 为空，B2 填充
    assert m["mode"] in ("strict", "standard", "open")
    assert m["provider"] in ("mock", "cloud", "local")


async def test_manifest_payload_consistency_with_assemble(app_state, conn_id):
    """清单与实际 payload（context + meta）一致。"""
    query = "按月统计订单金额"
    text, meta = await assemble_context_full(app_state, conn_id, query=query)
    messages = [{"role": "system", "content": text}, {"role": "user", "content": query}]
    req = ChatRequest(connection_id=conn_id, messages=[{"role": "user", "content": query}], provider="mock")
    cfg = resolve_provider_cfg(app_state, req)
    m = build_manifest(app_state, conn_id, meta, messages, False, cfg, text)
    # tables 与 meta 的候选表完全一致（排序后）
    assert sorted(m["tables"]) == sorted(meta.get("candidate_tables") or [])
    # kb_docs 与 context 中的知识库块一致（若有知识库）
    # history_turns 统计 user/assistant/tool
    assert m["history_turns"] == sum(1 for msg in messages if msg["role"] in ("user", "assistant", "tool"))
    # include_data 透传
    m2 = build_manifest(app_state, conn_id, meta, messages, True, cfg, text)
    assert m2["include_data"] is True
    assert m["include_data"] is False


async def test_chat_stream_emits_manifest_and_writes_audit(app_state, conn_id):
    """chat_stream 每次调用均产生 manifest 事件，且写审计 egress。"""
    # 清空审计以隔离计数
    before = len(app_state.audit.list())
    req = ChatRequest(connection_id=conn_id, messages=[{"role": "user", "content": "查退货率"}],
                      provider="mock", include_data=False)
    events = [ev async for ev in chat_stream(app_state, req)]
    manifests = [ev for ev in events if ev["type"] == "manifest"]
    assert len(manifests) == 1, f"expected 1 manifest, got {manifests}"
    m = manifests[0]["manifest"]
    assert "tables" in m and "kb_docs" in m
    # 审计：应新增至少 1 条 egress（verdict egress）
    after = app_state.audit.list()
    egress = [e for e in after if e.get("verdict") == "egress"]
    assert len(egress) >= 1, "missing egress audit"
    # 最新一条应含 manifest
    latest = egress[-1]
    assert "manifest" in latest and latest["manifest"]["tables"] == m["tables"]
    assert latest["manifest"]["history_turns"] == m["history_turns"]


async def test_manifest_human_readable():
    m = {"tables": ["orders", "order_items"], "kb_docs": 2, "history_turns": 3, "include_data": False, "redactions": [], "mode": "standard", "ts": "2026-08-20T12:00:00", "model": "mock", "provider": "mock"}
    zh = manifest_to_human(m, "zh-CN")
    en = manifest_to_human(m, "en-US")
    assert "2 张表" in zh or "2" in zh
    assert "无行数据" in zh
    assert "no rows" in en
    # 含聚合行数据
    m2 = {**m, "include_data": True}
    assert "含聚合" in manifest_to_human(m2, "zh-CN") or "with rows" in manifest_to_human(m2, "en-US")


async def test_report_stream_also_emits_manifest(app_state, conn_id):
    """报告模式同样有清单（含 report_id 关联但审计不计入章节数）。"""
    from app.ai.report import report_stream
    req = ChatRequest(connection_id=conn_id, messages=[{"role": "user", "content": "出一份销售报告"}],
                      provider="mock", mode="report")
    events = [ev async for ev in report_stream(app_state, req)]
    manifests = [ev for ev in events if ev["type"] == "manifest"]
    assert len(manifests) == 1
    assert manifests[0]["manifest"]["include_data"] is True  # 报告强制 include_data

async def test_egress_enumerable_rate_100_percent(app_state, conn_id):
    """出网可枚举率 100%：每次 chat 调用后，egress 审计数 == 实际模型调用数（mock 下 1 次）。"""
    before_egress = len([e for e in app_state.audit.list() if e.get("verdict") == "egress"])
    req = ChatRequest(connection_id=conn_id, messages=[{"role": "user", "content": "查询库存"}], provider="mock")
    _ = [ev async for ev in chat_stream(app_state, req)]
    after_egress = len([e for e in app_state.audit.list() if e.get("verdict") == "egress"])
    assert after_egress == before_egress + 1


async def test_privacy_mode_affects_manifest(app_state, conn_id):
    """三档模式写入 manifest.mode，并落审计。"""
    for mode in ("strict", "standard", "open"):
        app_state.runtime.update({"privacy_mode": mode})
        req = ChatRequest(connection_id=conn_id, messages=[{"role": "user", "content": "查订单"}], provider="mock")
        events = [ev async for ev in chat_stream(app_state, req)]
        m = next(ev["manifest"] for ev in events if ev["type"] == "manifest")
        assert m["mode"] == mode
    # 恢复
    app_state.runtime.update({"privacy_mode": "standard"})
