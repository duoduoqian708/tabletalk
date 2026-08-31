"""E5/§15.4 路由纯函数：route(action, modality) → skill_id。

穷举 8 条规则（查表 + general 兜底），可穷举单测。
skill id 沿用现有实现命名（scheduler），注释对齐设计文档命名（schedule）。
"""
from __future__ import annotations

from typing import Any

# 路由表：(action, modality) → skill_id；None = 任意
_ROUTE_RULES: list[tuple[tuple[str | None, str | None], str]] = [
    (("query", "answer"), "query"),
    (("query", "analyze"), "query"),
    (("query", "report"), "report"),
    (("query", "automate"), "scheduler"),   # 设计命名 schedule；实现沿用 scheduler
    (("write", None), "write"),
    (("ddl", None), "ddl"),                 # F4：独立 ddl 技能（§15.3，无执行工具）
    (("kb", None), "knowledge"),
    (("schedule", None), "scheduler"),
]

GENERAL_SKILL = "general"  # 兜底：unknown 组合 / 低置信


def route(action: str | None, modality: str | None = None) -> str:
    """任务剖面 → skill_id（纯函数）。

    - 精确匹配 action；modality 仅对 query 生效（升降级）
    - 无命中 → general 兜底（只读低危，永不拒绝也不闯祸）
    """
    action = action or "unknown"
    for (a, m), skill_id in _ROUTE_RULES:
        if a is not None and a != action:
            continue
        if m is not None and m != modality:
            continue
        return skill_id
    return GENERAL_SKILL


def route_effective(action: str | None, modality: str | None = None) -> str:
    """P1-1：route + enabled 拦截——被禁用的技能落到 general 兜底（只读低危）。

    route() 保持纯函数（穷举单测），enabled 状态在此组合层消费。
    地板技能（query/refusal）registry 层保证恒启用，不受影响。
    """
    from app.ai.skills.registry import get_skill

    sid = route(action, modality)
    s = get_skill(sid)
    if s is None or not s.enabled:
        return GENERAL_SKILL
    return sid


# ---------- F2：声明式配置消费（termination/degradation） ----------

_DEFAULT_MAX_TURNS = 6  # 与 loop.MAX_TURNS 兜底一致（技能未声明时）


def termination_max_turns(skill_id: str | None) -> int:
    """技能终止条件 max_turns；缺省回退全局默认（设计 §15.2 ④ 循环控制）。"""
    if not skill_id:
        return _DEFAULT_MAX_TURNS
    try:
        from app.ai.skills.registry import get_skill
        s = get_skill(skill_id)
        if s is None:
            return _DEFAULT_MAX_TURNS
        t = getattr(s, "termination", None) or {}
        v = t.get("max_turns")
        if isinstance(v, int) and v > 0:
            return v
    except Exception:
        pass
    return _DEFAULT_MAX_TURNS


def degradation_for(skill_id: str | None) -> str:
    """技能降级说明（路由命中但能力受限时提示用户）。"""
    if not skill_id:
        return ""
    try:
        from app.ai.skills.registry import get_skill
        s = get_skill(skill_id)
        return getattr(s, "degradation", "") or ""
    except Exception:
        return ""