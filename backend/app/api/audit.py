"""审计日志路由。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.state import get_state

router = APIRouter(prefix="/api/v1", tags=["audit"])


@router.get("/audit")
async def audit_log(
    connection: str | None = None,
    origin: str | None = None,
    tier: str | None = None,
    verdict: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    from_ts: str | None = None,
    to_ts: str | None = None,
    report_id: str | None = None,
) -> dict:
    state = get_state()
    full = state.audit.list(
        connection=connection, origin=origin, tier=tier, verdict=verdict,
        from_ts=from_ts, to_ts=to_ts, report_id=report_id,
    )
    total = len(full)
    window = full[::-1][offset:offset + limit]  # 最新在前
    return {"count": total, "entries": window}


@router.get("/audit/summary")
async def audit_summary(
    connection: str | None = None,
    verdict: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> dict:
    """信号区聚合：按判定/来源/层级计数 + 拦截率 + AI 尝试 DDL 数。"""
    state = get_state()
    full = state.audit.list(
        connection=connection, verdict=verdict, from_ts=from_ts, to_ts=to_ts,
    )
    total = len(full)
    by_verdict: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    for e in full:
        v = e.get("verdict", "?")
        o = e.get("origin", "?")
        t = e.get("tier", "?")
        by_verdict[v] = by_verdict.get(v, 0) + 1
        by_origin[o] = by_origin.get(o, 0) + 1
        by_tier[t] = by_tier.get(t, 0) + 1
    blocked = by_verdict.get("block", 0)
    ai_ddl = sum(1 for e in full if e.get("origin") == "ai" and e.get("tier") == "ddl")
    review = by_verdict.get("review", 0)
    return {
        "total": total,
        "by_verdict": by_verdict,
        "by_origin": by_origin,
        "by_tier": by_tier,
        "blocked_rate": (blocked / total) if total else 0,
        "ai_ddl_count": ai_ddl,
        "review_count": review,
    }
