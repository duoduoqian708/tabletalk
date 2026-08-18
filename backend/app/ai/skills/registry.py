"""技能注册表：可注册 / 列表 / 按 id 获取；内置技能不可删除，自定义可插拔。

技能是平台扩展的单元——新增能力就是注册一个新 Skill（复用原子 Tool + 写剧本）。
"""
from __future__ import annotations

from app.ai.skills.builtin import BUILTIN_SKILLS
from app.ai.skills.skill import Skill

_skills: dict[str, Skill] = {}
_loaded = False


def _ensure_loaded() -> None:
    global _loaded
    if not _loaded:
        for s in BUILTIN_SKILLS():
            _skills[s.id] = s
        _loaded = True


def register_skill(skill: Skill) -> None:
    _ensure_loaded()
    _skills[skill.id] = skill


def get_skill(skill_id: str) -> Skill | None:
    _ensure_loaded()
    return _skills.get(skill_id)


def list_skills() -> list[Skill]:
    _ensure_loaded()
    return list(_skills.values())


def remove_skill(skill_id: str) -> bool:
    """仅允许移除自定义技能；内置技能不可删。"""
    _ensure_loaded()
    s = _skills.get(skill_id)
    if s is None or s.builtin:
        return False
    del _skills[skill_id]
    return True
