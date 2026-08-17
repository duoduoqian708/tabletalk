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
    """意图→模式分流：explicit 显式指定时直接返回（前端「报告」按钮绕过意图分类）；
    否则真实 LLM 下让模型判别 report vs query，mock 下用关键词回退。

    只做"是不是报告"这一层二分；具体领域标签仍走 classify_tags。
    """
    if explicit:
        return MODE_REPORT if explicit == MODE_REPORT else MODE_QUERY
    q = (question or "").strip()
    if not q:
        return MODE_QUERY
    rt = state.runtime.get()
    # mock 行为下：关键词回退（显式 mock 或 cloud 无 key 降级）
    if gw.is_effective_mock(rt.provider_config()):
        return MODE_REPORT if is_report_intent(q) else MODE_QUERY
    # 真实 LLM 下：判定 report vs query（保守：拿不准就当 query）
    try:
        provider = gw.build_provider(rt.provider_config())
        prompt = (
            "判断用户是要【一份分析报告】还是【单次数据查询】。\n"
            f"用户问题：{question}\n"
            "报告特征：要一份结构化的、含图表/结论的综合分析（如'出 X 报告'、'趋势'、'概览'、'总结'）。\n"
            "查询特征：问一个具体的数据问题，期望一个表格/数字（如'查 Y 最高的'、'按月看 Z'）。\n"
            "只返回一个词：report 或 query。拿不准返回 query。"
        )
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None)
        text = (resp.content or "").strip().lower()
        return MODE_REPORT if "report" in text and "query" not in text else MODE_QUERY
    except Exception:  # noqa: BLE001
        # 网关异常时降级为关键词
        return MODE_REPORT if is_report_intent(q) else MODE_QUERY


async def classify_tags(state: "AppState", conn_id: str, question: str) -> list[str]:
    lib = state.knowledge.tags(conn_id)["library"]
    confirmed = [t["name"] for t in lib if t["status"] == "confirmed"]
    if not confirmed or not question.strip():
        return []

    rt = state.runtime.get()
    if gw.is_effective_mock(rt.provider_config()):
        q = question.lower()
        return [t for t in confirmed if t.lower() in q or q in t.lower()]

    prompt = (
        f"用户问题：{question}\n"
        f"该数据源可用领域标签：{confirmed}\n"
        "判断这个问题属于哪 1~3 个领域，只从上面标签中选择，返回 JSON 数组（如 [\"订单\"]）。只返回 JSON。"
    )
    provider = gw.build_provider(rt.provider_config())
    resp = await provider.chat([{"role": "user", "content": prompt}], tools=None)
    text = (resp.content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x) for x in data if str(x) in confirmed]
    except Exception:
        pass
    return []
