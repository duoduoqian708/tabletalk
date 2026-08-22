"""意图→领域标签分类（LLM 判定；mock 关键词回退）。

使用：问题 → 从"已确认标签库"选 1~N 标签 → route_tables 取候选表。
只认已确认的标签——draft 标签不参与路由。
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from app.ai import gateway as gw

if TYPE_CHECKING:
    from app.state import AppState

# 报告意图关键词：自然语言路由把"要一份报告/分析/概览"分到 report 模式。
# mock 下用关键词回退；真实 LLM 下由 chat_loop 决策（见 classify_mode）。
_REPORT_KEYWORDS = ("报告", "报表", "出一份", "分析报告", "概览", "总结报告",
                    "趋势分析", "insights", "report", "dashboard")


def is_report_intent(question: str) -> bool:
    """粗判：是否表达"要一份报告"的意图。仅当 mode 未显式指定时用于自然语言路由。"""
    q = (question or "").strip().lower()
    if not q:
        return False
    return any(k.lower() in q for k in _REPORT_KEYWORDS)


MODE_QUERY = "query"
MODE_REPORT = "report"


async def classify_mode(state: "AppState", question: str, explicit: str | None = None) -> str:
    """薄封装：显式 mode 优先，否则委托 preflight（统一 owner）。

    保留二分接口供旧调用方；新链路直接用 preflight。
    不再内联 manifest/redact/audit，单次逻辑在 preflight 内。
    """
    if explicit:
        return MODE_REPORT if explicit == MODE_REPORT else MODE_QUERY
    q = (question or "").strip()
    if not q:
        return MODE_QUERY
    try:
        from app.ai.preflight import preflight as _pf
        # 兼容：classify_mode 旧签名无 conn_id，用空连接走关键词/LLM（audit 用 __intent__）
        res = await _pf(state, "", q, history_tail=None)
        if res.intent == "report":
            return MODE_REPORT
        return MODE_QUERY
    except Exception:
        return MODE_REPORT if is_report_intent(q) else MODE_QUERY


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
