"""技能注册表：SQLite 持久化（tabletalk.db: skills），兼容旧 JSON。"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from app.ai.skills.builtin import BUILTIN_SKILLS
from app.ai.skills.skill import Skill
from app.ai.tools.registry import TOOL_SCHEMAS
from app.core.system_db import get_conn, init_system_db

_skills: dict[str, Skill] = {}
_custom: dict[str, Skill] = {}
_custom_path: Path | None = None
_data_dir: Path | None = None
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
    global _custom_path, _data_dir
    _ensure_loaded()
    _data_dir = Path(data_dir)
    init_system_db(_data_dir)
    _custom_path = _data_dir / "skills.json"
    _custom.clear()
    # 优先从 DB 读
    try:
        con = get_conn(_data_dir)
        cur = con.execute("SELECT data FROM skills")
        rows = cur.fetchall()
        con.close()
        if rows:
            for (data_json,) in rows:
                try:
                    d = json.loads(data_json)
                    s = Skill.from_dict(d)
                    if not s.builtin and s.id not in _skills:
                        _custom[s.id] = s
                except Exception:
                    continue
            return
    except Exception:
        pass
    # 回退：旧 JSON 迁移
    if not _custom_path.exists():
        return
    try:
        data = json.loads(_custom_path.read_text(encoding="utf-8"))
        for d in data.get("skills", []):
            try:
                s = Skill.from_dict(d)
                if not s.builtin and s.id not in _skills:
                    _custom[s.id] = s
            except Exception:
                continue
        if _custom:
            _save_custom()
            try:
                bak = _custom_path.with_suffix(".json.bak")
                if not bak.exists():
                    _custom_path.rename(bak)
            except OSError:
                pass
    except Exception:
        pass


def _save_custom() -> None:
    if _data_dir is None:
        return
    try:
        con = get_conn(_data_dir)
        con.execute("DELETE FROM skills")
        for s in _custom.values():
            con.execute(
                "INSERT INTO skills (id, data) VALUES (?,?)",
                (s.id, json.dumps(s.to_dict(), ensure_ascii=False)),
            )
        con.commit()
        con.close()
    except Exception:
        pass
    # 旧文件归档
    if _custom_path and _custom_path.exists():
        try:
            bak = _custom_path.with_suffix(".json.bak")
            if not bak.exists():
                _custom_path.rename(bak)
        except OSError:
            pass


def register_skill(skill: Skill) -> None:
    _ensure_loaded()
    _skills[skill.id] = skill


def register_custom(skill: Skill) -> Skill:
    _ensure_loaded()
    if not skill.id or skill.id in _skills:
        skill.id = _new_id()
    _custom[skill.id] = skill
    _save_custom()
    return skill


def update_skill(skill_id: str, patch: dict) -> Skill | None:
    _ensure_loaded()
    target = _skills.get(skill_id) or _custom.get(skill_id)
    if target is None:
        return None
    # 地板技能不可禁用（T1.2/08§4.5）：query/refusal 恒为 enabled
    if "enabled" in patch and patch["enabled"] is False and skill_id in {"query", "refusal"}:
        # 保持 enabled=True，不落库
        patch = {k: v for k, v in patch.items() if k != "enabled"}
        if not patch:
            return target
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


_org_disabled: set[str] = set()


def is_org_disabled(skill_id: str) -> bool:
    return skill_id in _org_disabled


def set_org_disabled(skill_id: str, disabled: bool) -> None:
    if disabled:
        _org_disabled.add(skill_id)
    else:
        _org_disabled.discard(skill_id)


def _tool_names() -> set[str]:
    return {t["function"]["name"] for t in TOOL_SCHEMAS}


def validate_skill(name: str, description: str, tools: list[str], triggers: list[str], read_only: bool) -> list[str]:
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
    """技能 → 可用工具集。

    - skill_id 为空：全量工具（默认查询面）
    - 技能不存在：全量工具回退（兼容旧契约，未知技能不额外收窄）
    - 技能存在且工具集为空：**零工具**（铁律 3·禁止靠缺席——refusal 等无工具技能
      模型拿不到任何动词，从契约层杜绝越权。空列表绝不可当"全量"）
    - 技能存在且工具集非空：交集过滤（只收窄不扩权）
    """
    if not skill_id:
        return list(TOOL_SCHEMAS)
    s = get_skill(skill_id)
    if s is None:
        return list(TOOL_SCHEMAS)
    if not s.enabled:
        return []  # P1-1：禁用技能零工具（铁律 3·禁止靠缺席）
    names = set(s.tools)
    if not names:
        return []
    return [t for t in TOOL_SCHEMAS if t["function"]["name"] in names]
