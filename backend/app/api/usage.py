"""用量统计 — E3 统一 AI 出口（组织级模型配置，用量按成员统计）"""
from __future__ import annotations

from collections import Counter
from fastapi import APIRouter

from app.state import get_state

router = APIRouter(prefix="/api/v1", tags=["usage"])

@router.get("/usage")
async def usage():
    state = get_state()
    # 统计审计中的 egress 按用户（若有 user_id，否则按 origin）
    entries = state.audit.list()
    by_user: Counter = Counter()
    by_model: Counter = Counter()
    for e in entries:
        if e.get("verdict") == "egress":
            # 优先用 manifest 中的 model/provider，若无则用 sql 前缀
            m = e.get("manifest", {})
            model = m.get("model") or m.get("provider") or "unknown"
            by_model[model] += 1
            # user_id 若有则按 user，否则按 connection
            user = e.get("user_id") or e.get("origin") or "unknown"
            by_user[user] += 1
    return {"by_user": dict(by_user), "by_model": dict(by_model), "total_egress": sum(by_model.values())}
