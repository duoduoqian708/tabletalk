"""用量统计 — E3 统一 AI 出口（组织级模型配置，用量按成员统计）"""
from __future__ import annotations

from collections import Counter
from fastapi import APIRouter

from app.state import get_state

router = APIRouter(prefix="/api/v1", tags=["usage"])

@router.get("/usage")
async def usage():
    state = get_state()
    entries = state.audit.list()
    by_user: Counter = Counter()
    by_model: Counter = Counter()
    total_tokens = 0
    for e in entries:
        if e.get("verdict") == "egress":
            m = e.get("manifest", {})
            model = m.get("model") or m.get("provider") or "unknown"
            by_model[model] += 1
            user = e.get("user_id") or e.get("origin") or "unknown"
            by_user[user] += 1
            # token 计数（若 audit 中有 prompt_tokens）
            total_tokens += int(e.get("prompt_tokens") or e.get("manifest", {}).get("prompt_tokens") or 0)
            total_tokens += int(e.get("completion_tokens") or e.get("manifest", {}).get("completion_tokens") or 0)
    return {"by_user": dict(by_user), "by_model": dict(by_model), "total_egress": sum(by_model.values()), "total_tokens": total_tokens}
