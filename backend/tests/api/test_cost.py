"""成本 API 测试：sessions 聚合 / __none__（无会话系统调用）分组钻取 / summary 不再 422。

回归点：cost 路由曾用裸 `request` 参数被 FastAPI 当必填 query → 全部 422，页面永远空；
修复后系统调用（KB 构建/嵌入）聚合为 __none__ 组并可钻取。
"""
from __future__ import annotations

from app.ai.llm_log import LlmCallLog
from app.config import get_env


def _log(session_id, skill="query", conn_id="c", inp=10, out=20) -> None:
    LlmCallLog(get_env().data_dir).log(
        conn_id=conn_id, skill=skill, model="m", provider="cloud",
        session_id=session_id, request_json={"messages": []}, response_json={"usage": {}},
        input_tokens=inp, output_tokens=out, elapsed_ms=5,
    )


async def test_cost_sessions_group_none_system_calls(client):
    """带会话的按会话聚合；无会话的（KB/嵌入）归并 __none__ 组。"""
    _log("sess_a")
    _log("sess_a")
    _log(None, skill="kb-annotation")
    _log(None, skill="kb-graph")
    r = await client.get("/api/v1/cost/sessions")
    assert r.status_code == 200, "裸 request 参数已修为非必填，不应 422"
    body = r.json()
    assert body["ok"] is True
    groups = {row["session_id"]: row for row in body["sessions"]}
    assert groups["sess_a"]["calls"] == 2
    none = groups["__none__"]
    assert none["calls"] == 2
    assert "kb-annotation" in none["skills"] and "kb-graph" in none["skills"]


async def test_cost_session_drill_none_returns_system_calls(client):
    """钻取 __none__ → 返回全部无会话系统调用。"""
    _log(None, skill="kb-annotation")
    _log(None, skill="kb-tags")
    r = await client.get("/api/v1/cost/sessions/__none__")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and len(body["calls"]) == 2
    assert {c["skill"] for c in body["calls"]} == {"kb-annotation", "kb-tags"}


async def test_cost_summary_no_request_query_param(client):
    """summary 直接可调（修复前因裸 request 参数 422）。"""
    r = await client.get("/api/v1/cost/summary")
    assert r.status_code == 200
    assert r.json()["ok"] is True