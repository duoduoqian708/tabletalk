"""报告模式测试（mock provider，无 key 全流程）。

钉死三条不变式：
- 报告子查询只读：物理不挂 run_dml/draft_ddl，闸门非 ALLOW 即拦截并记审计。
- 每章子查询照常进审计，挂 report_id；GET /audit?report_id= 能回溯一整份报告。
- 澄清挂起：欠定义问题 → yield clarify + done 结束本轮；带回答重传 → 出计划 + 章节。
"""
from __future__ import annotations

import json

import pytest

from app.ai.intent import is_report_intent
from app.ai.report import _run_report_query, report_stream
from app.ai.schemas import ChatRequest


async def _collect(state, conn_id, messages, mode="report"):
    req = ChatRequest(connection_id=conn_id, messages=messages, provider="mock", mode=mode)
    return [ev async for ev in report_stream(state, req)]


def test_is_report_intent_keywords():
    assert is_report_intent("出一份 Q3 销售分析报告")
    assert is_report_intent("给我看下趋势分析")
    assert is_report_intent("出个 insights dashboard")
    assert not is_report_intent("查退货率最高的 10 个商品")
    assert not is_report_intent("")


async def test_report_clarify_then_plan_with_answer(app_state, conn_id):
    """欠定义问题（无时间范围/口径）→ 先 yield clarify 后 done；
    带澄清回答重传 → 出 plan + 多 section + narration + report_done。"""
    # 第一轮：欠定义
    msgs = [{"role": "user", "content": "出一份报告"}]
    ev1 = await _collect(app_state, conn_id, msgs)
    types = [e["type"] for e in ev1]
    assert "report_start" in types
    assert types.count("clarify") >= 1, types
    assert types[-1] == "done"   # 澄清轮以 done 结束（未出计划）

    # 第二轮：带上回答重传 → 应出 plan + sections + narration + report_done
    msgs2 = [
        {"role": "user", "content": "出一份报告"},
        {"role": "system", "name": "clarify", "content": "你要分析的时间范围是？"},
        {"role": "user", "content": "2026 全年"},
    ]
    ev2 = await _collect(app_state, conn_id, msgs2)
    types2 = [e["type"] for e in ev2]
    plan_ev = next(e for e in ev2 if e["type"] == "plan")
    assert len(plan_ev["sections"]) >= 2
    sections = [e for e in ev2 if e["type"] == "section"]
    assert len(sections) >= 2
    # 每章都 ok 且带聚合数据
    for s in sections:
        assert s["ok"] is True
        assert s["row_count"] >= 0
        assert "chart" in s
    assert "narration" in types2
    assert "report_done" in types2
    assert types2[-1] == "done"


async def test_report_sections_audited_with_report_id(app_state, conn_id):
    """每章子查询进审计，挂同一 report_id；GET /audit?report_id= 一份报告全回溯。"""
    msgs = [
        {"role": "user", "content": "2026 全年销售分析报告"},
        {"role": "system", "name": "clarify", "content": "聚焦哪个业务域？"},
        {"role": "user", "content": "销售"},
    ]
    evs = await _collect(app_state, conn_id, msgs)
    report_start = next(e for e in evs if e["type"] == "report_start")
    rid = report_start["report_id"]
    sections = [e for e in evs if e["type"] == "section"]
    # 审计里有这些查询，且都挂同一 report_id
    audit = app_state.audit.list(report_id=rid)
    assert len(audit) == len(sections)
    assert all(a["report_id"] == rid for a in audit)
    assert all(a["verdict"] == "allow" for a in audit)   # 只读放行
    assert all(a["origin"] == "ai" for a in audit)


async def test_report_run_query_blocks_writes(app_state, conn_id):
    """报告子查询走 _run_report_query：DDL/写语句闸门非 ALLOW 即拦截并记审计（block）。"""
    cfg = app_state.connections.get(conn_id)
    qr = await _run_report_query(
        app_state, conn_id, "rep_test", "DROP TABLE orders", "r_drop"
    )
    assert qr["ok"] is False
    # 审计记一条 block，挂 report_id
    a = app_state.audit.list(report_id="rep_test")
    assert any(x["verdict"] == "block" for x in a)


async def test_report_through_api_sse(client, conn_id):
    """经 /ai/chat SSE 端到端：报告事件流可被前端解析，且会话落库含 report kind 消息。"""
    # 提一带回答的报告请求（绕过澄清，直接出报告）
    payload = {
        "connection_id": conn_id,
        "session_id": "rep-sess-1",
        "mode": "report",
        "provider": "mock",
        "messages": [
            {"role": "user", "content": "2026 全年销售分析报告"},
            {"role": "system", "name": "clarify", "content": "聚焦业务域？"},
            {"role": "user", "content": "销售"},
        ],
    }
    r = await client.post("/api/v1/ai/chat", json=payload)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    types: list[str] = []
    async for line in r.aiter_lines():
        if line.startswith("data: ") and line != "data: [DONE]":
            ev = json.loads(line[6:])
            types.append(ev["type"])
    assert "report_start" in types
    assert "plan" in types
    assert "section" in types
    assert "narration" in types
    assert "report_done" in types
    # 会话落库含报告消息（kind=report）
    detail = (await client.get("/api/v1/chat/sessions/rep-sess-1")).json()
    kinds = {m["kind"] for m in detail["messages"]}
    assert "report" in kinds
    assert any(m["role"] == "user" for m in detail["messages"])


async def test_report_intent_classification_query_mode(app_state, conn_id):
    """单查询走 query 模式（非报告）——classify_mode 不误判普通查询为 report。"""
    from app.ai.intent import MODE_QUERY, MODE_REPORT, classify_mode

    qmode = await classify_mode(app_state, "查退货率最高的 10 个商品")
    assert qmode == MODE_QUERY
    rmode = await classify_mode(app_state, "出一份 Q3 销售分析报告")
    assert rmode == MODE_REPORT
    # 显式 explicit 优先
    assert await classify_mode(app_state, "随便", explicit=MODE_REPORT) == MODE_REPORT
    assert await classify_mode(app_state, "出报告", explicit=MODE_QUERY) == MODE_QUERY


async def test_audit_filter_by_report_id(client, conn_id):
    """GET /api/v1/audit?report_id= 只返回该报告的审计。"""
    # 先制造一条报告审计 == 0（重测最常见，属于 test_query_run_does_not_touch_sessions 类）。
    before = (await client.get("/api/v1/audit", params={"report_id": "rep_xxx_notexist"})).json()
    assert before["count"] == 0   # 不存在的 report_id 返回空（不影响其他审计）
    # 跑一份报告
    payload = {
        "connection_id": conn_id, "mode": "report", "provider": "mock",
        "messages": [
            {"role": "user", "content": "2026 全年销售额报告"},
            {"role": "system", "name": "clarify", "content": "范围？"},
            {"role": "user", "content": "全年"},
        ],
    }
    r = await client.post("/api/v1/ai/chat", json=payload)
    assert r.status_code == 200
    # 报告审计里都带 report_id
    entries = (await client.get("/api/v1/audit")).json()["entries"]
    with_rid = [e for e in entries if e.get("report_id")]
    assert with_rid, "报告查询未写 audit"
    rid = with_rid[-1]["report_id"]
    only_report = (await client.get("/api/v1/audit", params={"report_id": rid})).json()
    assert only_report["count"] >= 2
    assert all(e["report_id"] == rid for e in only_report["entries"])