"""审计日志路由。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.state import get_state

router = APIRouter(prefix="/api/v1", tags=["audit"])


@router.get("/audit")
async def audit_log(
    connection: str | None = None,
    verdict: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    from_ts: str | None = None,
    to_ts: str | None = None,
    report_id: str | None = None,
) -> dict:
    state = get_state()
    entries = state.audit.list(
        connection=connection, verdict=verdict, limit=limit,
        from_ts=from_ts, to_ts=to_ts, report_id=report_id,
    )
    return {"count": len(entries), "entries": entries}
