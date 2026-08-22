"""意图调度：用户问题 → 从技能注册表选一个技能。

WS1 后：preflight 产出 {intent, tags}，本模块做 intent→skill 映射 + enabled 感知 + 降级。
保留 _trigger_match / dispatch_skill 供旧链路兼容，新链路走 resolve_skill。
"""
from __future__ import annotations

from app.ai import gateway as gw
from app.ai.intent import is_report_intent
from app.ai.skills.registry import get_skill, list_enabled_skills, list_skills

_DEFAULT = "query"

INTENT_TO_SKILL: dict[str, str] = {
    "query": "query",
    "report": "report",
    "schema": "schema",
    "write": "write",
    "ddl": "ddl",
    "audit": "audit_qa",
    "offtopic": "refusal",
}

FLOOR_SKILLS = {"query", "refusal"}


def _trigger_match(q: str) -> str | None:
    """启用技能的触发词关键词匹配（确定性路由，mock 与真实模型共用）。"""
    ql = (q or "").lower()
    for s in list_enabled_skills():
        if s.id == "query":
            continue
        for t in s.triggers:
            if t and t.lower() in ql:
                return s.id
    return None


def resolve_skill_from_intent(intent: str) -> tuple[str, bool, str | None]:
    """intent → skill_id + 是否降级 + 降级文案。

    - 地板技能（query/refusal）永远可用，不可禁用
    - 目标 skill 未注册：回 query（不算降级）
    - 目标 skill 已注册但 disabled：返回降级文案
    """
    skill_id = INTENT_TO_SKILL.get(intent or "query", "query")
    # 未注册的 skill：回 query
    if get_skill(skill_id) is None:
        # offtopic/refusal 若未注册（实现期），同样回 query 不降级
        return "query", False, None
    # 检查 enabled
    enabled_ids = {s.id for s in list_enabled_skills()}
    # 地板永远认为 enabled
    if skill_id in FLOOR_SKILLS:
        return skill_id, False, None
    if skill_id in enabled_ids:
        return skill_id, False, None
    # 被禁用：降级
    msg = "此能力已关闭，可在设置中开启"
    return skill_id, True, msg


async def dispatch_skill(state, question: str) -> str:
    """兼容旧链路：仍支持触发词 + mock/report 回退；新 preflight 链路不走此函数。"""
    q = (question or "").strip()
    if not q:
        return _DEFAULT
    hit = _trigger_match(q)
    if hit:
        return hit
    rt = state.runtime.get()
    if gw.is_effective_mock(rt.provider_config()):
        if is_report_intent(q):
            return "report"
        return _DEFAULT
    skills = [s for s in list_enabled_skills() if s.id != "query"]  # query 是兜底，不参与选择
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
