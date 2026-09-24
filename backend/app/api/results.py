"""统一报告结果 API：列表（不含正文）+ 详情（含正文）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.state import get_state

router = APIRouter(prefix="/api/v1/results", tags=["results"])


@router.get("")
async def list_results(source: str = "", group_id: str = "", limit: int = 50) -> dict[str, Any]:
    state = get_state()
    limit = max(1, min(int(limit or 50), 200))
    items = state.results.list_results(
        source=source.strip() or None, group_id=group_id.strip() or None, limit=limit)
    return {"ok": True, "results": items, "count": len(items)}


@router.get("/{rid}")
async def get_result(rid: str) -> dict[str, Any]:
    state = get_state()
    r = state.results.get_result(rid.strip())
    if r is None:
        raise HTTPException(status_code=404, detail=f"报告不存在: {rid}")
    return {"ok": True, "result": r}
