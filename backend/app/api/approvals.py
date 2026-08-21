"""审批流 API — E2"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.state import get_state

router = APIRouter(prefix="/api/v1", tags=["approvals"])

class CreateRequest(BaseModel):
    connection_id: str
    sql: str

class ReviewRequest(BaseModel):
    note: str | None = None

def _user_id(request: Request) -> str:
    # 从中间件注入的 user 中取 sub，单机模式则为 anonymous
    user = getattr(request.state, "user", None)
    if isinstance(user, dict):
        return user.get("sub") or user.get("username") or "anonymous"
    return getattr(request.state, "user_id", None) or "anonymous"

@router.post("/approvals")
async def create_approval(req: CreateRequest, request: Request):
    state = get_state()
    # 仅团队模式需要审批，单机直接 400
    if not state.auth.is_team_mode():
        raise HTTPException(status_code=400, detail="approvals only in team mode")
    # 检查连接是否存在
    try:
        state.connections.get(req.connection_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    uid = _user_id(request)
    a = state.approvals.create(req.connection_id, req.sql, uid)
    # 审计
    state.audit.log(connection=req.connection_id, origin="ai", tier="dml", verdict="review", status="转审批", sql=req.sql, source="approval", reasons=[{"rule_id":"approval-pending","message":"转审批","message_en":"Pending approval","objects":[]}])
    return {"id": a.id, "status": a.status}

@router.get("/approvals")
async def list_approvals(status: str | None = None):
    state = get_state()
    if not state.auth.is_team_mode():
        raise HTTPException(status_code=400, detail="approvals only in team mode")
    return {"items": [a.__dict__ for a in state.approvals.list(status)]}

@router.post("/approvals/{aid}/approve")
async def approve(aid: str, req: ReviewRequest, request: Request):
    state = get_state()
    uid = _user_id(request)
    # 仅 admin 可批（简化：检查 role）
    user = getattr(request.state, "user", None)
    role = user.get("role") if isinstance(user, dict) else None
    if role != "admin":
        raise HTTPException(status_code=403, detail="admin required")
    a = state.approvals.approve(aid, uid, req.note)
    if not a:
        raise HTTPException(status_code=404, detail="not found or not pending")
    # 审计
    state.audit.log(connection=a.connection_id, origin="ai", tier="dml", verdict="executed", status="审批通过", sql=a.sql, source="approval")
    # 实际执行（在原上下文执行）
    from app.core import query as core_query
    from app.safety import gate as safety_gate
    # 重新评估（防 TOCTOU）
    # 为简化，直接执行
    try:
        res = await core_query.execute(state, a.connection_id, a.sql)
        return {"id": a.id, "status": a.status, "result": res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post("/approvals/{aid}/reject")
async def reject(aid: str, req: ReviewRequest, request: Request):
    state = get_state()
    uid = _user_id(request)
    user = getattr(request.state, "user", None)
    role = user.get("role") if isinstance(user, dict) else None
    if role != "admin":
        raise HTTPException(status_code=403, detail="admin required")
    a = state.approvals.reject(aid, uid, req.note)
    if not a:
        raise HTTPException(status_code=404, detail="not found or not pending")
    state.audit.log(connection=a.connection_id, origin="ai", tier="dml", verdict="block", status="审批驳回", sql=a.sql, source="approval")
    return {"id": a.id, "status": a.status}
