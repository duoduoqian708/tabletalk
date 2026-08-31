"""意图→技能映射（2026-09：意图意图由 decompose 的 TaskPlan 驱动，本模块只做映射+降级）。"""
from __future__ import annotations

from app.ai.skills.registry import get_skill, list_enabled_skills

_DEFAULT = "query"

# 新意图集：query/write/report/knowledge/scheduler/offtopic（v2 设计文档）
INTENT_TO_SKILL: dict[str, str] = {
    "query": "query",
    "write": "write",
    "report": "report",
    "knowledge": "knowledge",
    "scheduler": "scheduler",
    "offtopic": "refusal",
}

FLOOR_SKILLS = {"query", "refusal"}


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
