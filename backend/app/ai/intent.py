"""意图→领域标签分类（2026-09：意图识别归 decompose LLM，本模块仅剩常量 + tags 封装）。"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from app.ai import gateway as gw

if TYPE_CHECKING:
    from app.state import AppState

MODE_QUERY = "query"
MODE_REPORT = "report"
MODE_QUERY = "query"
MODE_REPORT = "report"


async def classify_tags(state: "AppState", conn_id: str, question: str) -> list[str]:
    """薄封装：委托 preflight 返回 tags（统一 owner）。"""
    try:
        from app.ai.preflight import preflight as _pf
        res = await _pf(state, conn_id, question, history_tail=None)
        return res.tags
    except Exception:
        try:
            lib = state.knowledge.tags(conn_id)["library"]
            confirmed = [t["name"] for t in lib if t["status"] == "confirmed"]
            ql = (question or "").lower()
            return [t for t in confirmed if t.lower() in ql or ql in t.lower()]
        except Exception:
            return []
