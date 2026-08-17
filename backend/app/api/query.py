"""查询路由：跑 SQL（走闸门）、取消、格式化。

闸门流程：
- ALLOW（读）→ 直接执行
- BLOCK → 拦截，返回原因 + 建议，不执行
- REVIEW（写/手动 DDL）→ 未 confirm 返回 verdict+preview；confirm 后重新评估仍须 REVIEW 才执行
"""
from __future__ import annotations

import time

import sqlglot
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core import query as core_query
from app.safety import gate as safety_gate
from app.safety.models import Origin, Verdict
from app.state import get_state

router = APIRouter(prefix="/api/v1", tags=["query"])


class QueryRequest(BaseModel):
    connection_id: str
    sql: str
    origin: str = "manual"        # manual | ai
    confirm: bool = False
    limit: int | None = None
    offset: int | None = None
    count_total: bool = False


class CancelRequest(BaseModel):
    connection_id: str


class FormatRequest(BaseModel):
    sql: str
    dialect: str = "sqlite"


def _ctx(state, conn_id: str):
    try:
        cfg = state.connections.get(conn_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return cfg, safety_gate.sqlglot_dialect_for(cfg.dialect)


@router.post("/query")
async def run_query(req: QueryRequest) -> dict:
    state = get_state()
    cfg, dialect = _ctx(state, req.connection_id)
    origin = Origin.AI if req.origin == "ai" else Origin.MANUAL
    assessment = safety_gate.assess_sql(req.sql, dialect, origin)
    t0 = time.monotonic()

    # 只读连接硬边界：写/DDL 一律 BLOCK（不降级为 REVIEW 确认）
    if cfg.read_only and assessment.verdict != Verdict.ALLOW:
        elapsed = round((time.monotonic() - t0) * 1000, 1)
        state.audit.log(connection=cfg.name, origin=origin.value, tier=assessment.tier.value,
                        verdict="block", status="只读连接拦截", sql=req.sql, elapsed_ms=elapsed)
        return {
            "verdict": "block", "tier": assessment.tier.value,
            "reason": "该连接标记为只读，禁止写操作与结构变更",
            "suggestions": [],
            "elapsed_ms": elapsed,
        }

    if assessment.verdict == Verdict.ALLOW:
        res = await core_query.execute(state, req.connection_id, req.sql, req.limit, req.offset)
        elapsed = round((time.monotonic() - t0) * 1000, 1)
        total = None
        if req.count_total:
            total = await core_query.count_total(state, req.connection_id, req.sql)
        state.audit.log(connection=cfg.name, origin=origin.value, tier="read", verdict="allow",
                        status="放行", sql=req.sql, elapsed_ms=elapsed)
        return {**res, "verdict": "allow", "tier": "read", "reason": "", "total": total,
                "suggestions": []}

    if assessment.verdict == Verdict.BLOCK:
        elapsed = round((time.monotonic() - t0) * 1000, 1)
        state.audit.log(connection=cfg.name, origin=origin.value, tier=assessment.tier.value,
                        verdict="block", status="拦截", sql=req.sql, elapsed_ms=elapsed)
        return {
            "verdict": "block", "tier": assessment.tier.value,
            "reason": "; ".join(assessment.reasons),
            "suggestions": safety_gate.suggest_safe(req.sql, dialect, origin),
            "elapsed_ms": elapsed,
        }

    # REVIEW
    if not req.confirm:
        preview = None
        if assessment.tier.value == "dml":
            preview = await safety_gate.preview_rows(state, req.connection_id, req.sql, dialect)
        elapsed = round((time.monotonic() - t0) * 1000, 1)
        state.audit.log(connection=cfg.name, origin=origin.value, tier=assessment.tier.value,
                        verdict="review", status="需确认", sql=req.sql, elapsed_ms=elapsed)
        return {
            "verdict": "review", "tier": assessment.tier.value,
            "reason": "; ".join(assessment.reasons),
            "preview_rows": preview, "needs_confirm": True,
            "elapsed_ms": elapsed,
        }

    # confirm：重新评估，仍须 REVIEW（不是 BLOCK）才执行
    reassess = safety_gate.assess_sql(req.sql, dialect, origin)
    if reassess.verdict == Verdict.BLOCK:
        return {
            "verdict": "block", "tier": reassess.tier.value,
            "reason": "; ".join(reassess.reasons),
            "suggestions": safety_gate.suggest_safe(req.sql, dialect, origin),
        }
    res = await core_query.execute(state, req.connection_id, req.sql, req.limit, req.offset)
    elapsed = round((time.monotonic() - t0) * 1000, 1)
    status = "已确认执行" if assessment.tier.value == "dml" else "已执行"
    state.audit.log(connection=cfg.name, origin=origin.value, tier=assessment.tier.value,
                    verdict=assessment.verdict.value, status=status, sql=req.sql, elapsed_ms=elapsed)
    return {
        "verdict": "executed", "tier": assessment.tier.value,
        "reason": "已确认执行",
        "affected_rows": res.get("affected_rows"),
        "row_count": res.get("row_count"),
        "elapsed_ms": elapsed,
    }


@router.post("/query/cancel")
async def cancel_query(req: CancelRequest) -> dict:
    state = get_state()
    _ctx(state, req.connection_id)
    n = core_query.cancel(req.connection_id)
    return {"cancelled": n}


@router.post("/sql/format")
async def format_sql(req: FormatRequest) -> dict:
    try:
        pretty = sqlglot.transpile(req.sql, read=req.dialect, pretty=True)
        return {"formatted": "\n".join(pretty)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"格式化失败: {e}") from e
