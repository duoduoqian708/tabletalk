"""意图调度：用户问题 → 从技能注册表选一个技能。

真实 LLM 下从非 query 技能里选；mock/降级用关键词；兜底回 query（保证永远有得回答）。
"""
from __future__ import annotations

from app.ai import gateway as gw
from app.ai.intent import is_report_intent
from app.ai.skills.registry import list_skills

_DEFAULT = "query"


async def dispatch_skill(state, question: str) -> str:
    q = (question or "").strip()
    if not q:
        return _DEFAULT
    rt = state.runtime.get()
    if gw.is_effective_mock(rt.provider_config()):
        if is_report_intent(q):
            return "report"
        return _DEFAULT
    skills = [s for s in list_skills() if s.id != "query"]  # query 是兜底，不参与选择
    if not skills:
        return _DEFAULT
    desc = "\n".join(f"- {s.id}: {s.description}" for s in skills)
    prompt = (
        f"用户问题：{question}\n"
        f"可用技能（除默认查询外）：\n{desc}\n"
        "返回最匹配的一个技能 id；都不匹配或拿不准返回 query。只返回一个词。"
    )
    try:
        provider = gw.build_provider(rt.provider_config())
        resp = await provider.chat([{"role": "user", "content": prompt}], tools=None)
        text = (resp.content or "").strip().lower()
        for s in skills:
            if s.id in text:
                return s.id
    except Exception:  # noqa: BLE001
        pass
    return _DEFAULT
