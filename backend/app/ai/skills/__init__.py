"""技能层：可插拔的技能注册表（Skill 剧本 × 原子 Tool）。

包结构：app.ai.skills.<skill_name>/ 每个技能独立子包（query/report/write_data/...），
公共基座在 app.ai.skills.base。
"""
from app.ai.skills.registry import get_skill, list_skills, register_skill, remove_skill
from app.ai.skills.skill import ScriptSpec, ScriptStep, Skill

__all__ = ["get_skill", "list_skills", "register_skill", "remove_skill",
           "Skill", "ScriptSpec", "ScriptStep"]
