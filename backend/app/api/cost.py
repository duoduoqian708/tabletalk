"""成本 API：/api/v1/cost — 趋势、会话聚合、调用明细。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/v1/cost", tags=["cost"])


def _get_log():
    from app.config import get_env
    from app.ai.llm_log import LlmCallLog
    return LlmCallLog(get_env().data_dir)


def _get_tracker():
    from app.config import get_env
    from app.ai.cost_tracker import CostTracker
    return CostTracker(get_env().data_dir)


@router.get("/summary")
async def cost_summary(from_ts: str | None = None, to_ts: str | None = None) -> dict[str, Any]:
    tracker = _get_tracker()
    return {"ok": True, **tracker.get_summary(from_ts, to_ts)}


@router.get("/daily")
async def cost_daily(from_ts: str | None = None, to_ts: str | None = None) -> dict[str, Any]:
    log = _get_log()
    daily = log.daily_agg(from_ts, to_ts)
    return {"ok": True, "daily": daily}


@router.get("/sessions")
async def cost_sessions(from_ts: str | None = None, to_ts: str | None = None) -> dict[str, Any]:
    """按 session 聚合 LLM 调用统计（无会话的系统调用归并 '__none__' 组）。"""
    log = _get_log()
    sessions = log.sessions_agg(from_ts, to_ts)
    return {"ok": True, "sessions": sessions}


@router.get("/sessions/{session_id}")
async def cost_session_calls(session_id: str, from_ts: str | None = None, to_ts: str | None = None) -> dict[str, Any]:
    """某 session 的所有 LLM 调用明细；'__none__' 表示无会话系统调用（KB 构建/嵌入）。"""
    log = _get_log()
    calls = log.session_calls(session_id, from_ts, to_ts)
    return {"ok": True, "session_id": session_id, "calls": calls}


@router.get("/calls/{call_id}")
async def cost_call_detail(call_id: int) -> dict[str, Any]:
    """单条 LLM 调用完整详情（含 request/response JSON）。"""
    log = _get_log()
    detail = log.call_detail(call_id)
    if not detail:
        raise HTTPException(404, "call not found")
    return {"ok": True, "call": detail}
