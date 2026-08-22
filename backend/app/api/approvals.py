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
        cfg = state.connections.get(req.connection_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    # 创建前先过闸门（防无效 SQL 进入审批队列；BLOCK 仍可入队但需审计标记）
    try:
        from app.safety import gate as _gate
        from app.safety.models import Origin as _Orig
        _dialect = _gate.sqlglot_dialect_for(cfg.dialect)
        _ass = _gate.assess_sql(req.sql, _dialect, _Orig.AI)
        if _ass.verdict.value == "block":
            # 仍允许入队，但 reasons 标记，避免静默
            pass
    except Exception:
        pass
    uid = _user_id(request)
    a = state.approvals.create(req.connection_id, req.sql, uid)
    # 审计（关联审批 id）
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
    # E2 修复：批准后执行必须重过安全闸门 + 只读检查（防 TOCTOU，击穿不变式）
    from app.core import query as core_query
    from app.safety import gate as safety_gate
    from app.safety.models import Origin, Verdict
    try:
        cfg = state.connections.get(a.connection_id)
        dialect = safety_gate.sqlglot_dialect_for(cfg.dialect)
        reassess = safety_gate.assess_sql(a.sql, dialect, Origin.AI)
        if reassess.verdict == Verdict.BLOCK:
            state.audit.log(connection=a.connection_id, origin="ai", tier=reassess.tier.value, verdict="block", status="审批执行拦截-闸门", sql=a.sql, source="approval", reasons=reassess.reasons)
            raise HTTPException(status_code=403, detail={"reason": "; ".join(r.get("message","") for r in reassess.reasons), "reasons": reassess.reasons})
        if cfg.read_only and reassess.verdict != Verdict.ALLOW:
            ro_reasons = [{"rule_id":"read-only","message":"该连接标记为只读，禁止写操作","message_en":"Connection is read-only","objects": reassess.tables}]
            state.audit.log(connection=a.connection_id, origin="ai", tier=reassess.tier.value, verdict="block", status="审批执行拦截-只读", sql=a.sql, source="approval", reasons=ro_reasons)
            raise HTTPException(status_code=403, detail={"reason": "read-only", "reasons": ro_reasons})
    except HTTPException:
        raise
    except Exception:
        pass
    # 生成回滚剧本（A4）供审计关联
    rollback = None
    rollback_ref = None
    try:
        from app.safety.rollback import build_rollback
        # 需要 dialect
        try:
            cfg2 = state.connections.get(a.connection_id)
            dialect2 = safety_gate.sqlglot_dialect_for(cfg2.dialect)
        except Exception:
            dialect2 = "sqlite"
        rollback = build_rollback(a.sql, dialect2)
        if rollback and rollback.get("backup_sql"):
            import hashlib as _hl
            rollback_ref = _hl.sha256(rollback["backup_sql"].encode()).hexdigest()[:12]
        elif rollback and rollback.get("rollback_sql"):
            import hashlib as _hl2
            rollback_ref = _hl2.sha256(rollback["rollback_sql"].encode()).hexdigest()[:12]
    except Exception:
        rollback = None
    # 审计（审批通过）
    state.audit.log(connection=a.connection_id, origin="ai", tier="dml", verdict="executed", status="审批通过", sql=a.sql, source="approval")
    # 实际执行（在原上下文执行）
    try:
        res = await core_query.execute(state, a.connection_id, a.sql)
        # 执行后追加一条带 rollback_ref 的审计（便于回溯）
        try:
            state.audit.log(connection=a.connection_id, origin="ai", tier="dml", verdict="executed", status="审批执行完成", sql=a.sql, source="approval", **({"rollback_ref": rollback_ref} if rollback_ref else {}))
        except Exception:
            pass
        out = {"id": a.id, "status": a.status, "result": res}
        if rollback:
            out["rollback"] = rollback
            if rollback_ref:
                out["rollback_ref"] = rollback_ref
        return out
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
