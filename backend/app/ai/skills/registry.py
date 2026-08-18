"""技能注册表：可注册 / 列表 / 按 id 获取；内置技能不可删除，自定义可插拔且持久化。

技能是平台扩展的单元——新增能力就是注册一个新 Skill（复用原子 Tool + 写剧本）。
自定义技能持久化到 data_dir/skills.json；内置技能不可删除，但可启用/禁用与组合工具。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from app.ai.skills.builtin import BUILTIN_SKILLS
from app.ai.skills.skill import Skill
from app.ai.tools.registry import TOOL_SCHEMAS

_skills: dict[str, Skill] = {}
_custom: dict[str, Skill] = {}
_custom_path: Path | None = None
_loaded = False


def _ensure_loaded() -> None:
    global _loaded
    if not _loaded:
        for s in BUILTIN_SKILLS():
            _skills[s.id] = s
        _loaded = True


def _new_id() -> str:
    return f"sk_{uuid.uuid4().hex[:10]}"


def load_custom(data_dir: str | Path) -> None:
    """从 data_dir/skills.json 加载自定义技能（重启后恢复）。"""
    global _custom_path
    _ensure_loaded()
    _custom_path = Path(data_dir) / "skills.json"
    _custom.clear()
    if not _custom_path.exists():
        return
    try:
        data = json.loads(_custom_path.read_text(encoding="utf-8"))
        for d in data.get("skills", []):
            s = Skill.from_dict(d)
            if not s.builtin and s.id not in _skills:
                _custom[s.id] = s
    except Exception:  # noqa: BLE001
        pass


def _save_custom() -> None:
    if _custom_path is None:
        return
    payload = {"skills": [s.to_dict() for s in _custom.values()]}
    _custom_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def register_skill(skill: Skill) -> None:
    _ensure_loaded()
    _skills[skill.id] = skill


def register_custom(skill: Skill) -> Skill:
    """登记自定义技能（带持久化）；返回实际登记的对象（id 由注册表生成）。"""
    _ensure_loaded()
    if not skill.id or skill.id in _skills:
        skill.id = _new_id()
    _custom[skill.id] = skill
    _save_custom()
    return skill


def update_skill(skill_id: str, patch: dict) -> Skill | None:
    """更新技能（启用/禁用、工具组合、提示词、触发词）。内置技能可改 enabled/tools/prompt，不可改 id。"""
    _ensure_loaded()
    target = _skills.get(skill_id) or _custom.get(skill_id)
    if target is None:
        return None
    d = target.to_dict()
    for k in ("name", "description", "system_prompt", "triggers", "enabled", "read_only"):
        if k in patch:
            d[k] = patch[k]
    if "tools" in patch:
        d["tools"] = [t for t in patch["tools"] if t in _tool_names()]
    updated = Skill.from_dict(d)
    updated.builtin = target.builtin
    if updated.builtin:
        _skills[skill_id] = updated
    else:
        _custom[skill_id] = updated
        _save_custom()
    return updated


def remove_skill(skill_id: str) -> bool:
    """仅允许移除自定义技能；内置技能不可删。"""
    _ensure_loaded()
    s = _skills.get(skill_id) or _custom.get(skill_id)
    if s is None or s.builtin:
        return False
    del _custom[skill_id]
    _save_custom()
    return True


def get_skill(skill_id: str) -> Skill | None:
    _ensure_loaded()
    return _skills.get(skill_id) or _custom.get(skill_id)


def list_skills() -> list[Skill]:
    _ensure_loaded()
    return list(_skills.values()) + list(_custom.values())


def list_enabled_skills() -> list[Skill]:
    return [s for s in list_skills() if s.enabled]


def _tool_names() -> set[str]:
    return {t["function"]["name"] for t in TOOL_SCHEMAS}


def validate_skill(name: str, description: str, tools: list[str], triggers: list[str], read_only: bool) -> list[str]:
    """组合校验：工具必须存在；只读技能禁用写工具。"""
    errors: list[str] = []
    if not name or not name.strip():
        errors.append("技能名称不能为空")
    if not tools:
        errors.append("至少勾选一个工具")
    known = _tool_names()
    unknown = [t for t in tools if t not in known]
    if unknown:
        errors.append(f"包含未注册的工具：{', '.join(unknown)}")
    if read_only:
        write_tools = [t for t in tools if t not in {"get_schema", "describe_table", "run_query"}]
        if write_tools:
            errors.append(f"只读技能不能包含写工具：{', '.join(write_tools)}")
    if triggers and any(not t.strip() for t in triggers):
        errors.append("触发词不能为空串")
    return errors


def skill_tool_schemas(skill_id: str | None) -> list[dict]:
    """按技能的工具组合过滤函数调用工具集（组合生效；过滤只会收窄，安全闸门仍兜底）。"""
    if not skill_id:
        return list(TOOL_SCHEMAS)
    s = get_skill(skill_id)
    if s is None or not s.tools:
        return list(TOOL_SCHEMAS)
    names = set(s.tools)
    return [t for t in TOOL_SCHEMAS if t["function"]["name"] in names]
