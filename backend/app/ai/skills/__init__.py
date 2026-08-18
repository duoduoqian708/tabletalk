"""技能层：可插拔的技能注册表（Skill 剧本 × 原子 Tool）。"""
from app.ai.skills.registry import get_skill, list_skills, register_skill, remove_skill
from app.ai.skills.skill import ScriptSpec, ScriptStep, Skill

__all__ = ["get_skill", "list_skills", "register_skill", "remove_skill",
           "Skill", "ScriptSpec", "ScriptStep"]
