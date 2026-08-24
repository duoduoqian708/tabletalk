"""技能广场 API：技能清单（含工具目录）/ 新建自定义技能 / 更新（启用·组合·提示词）/ 删除。

安全约束：
- 工具组合必须来自注册表（未知工具拒绝）；只读技能禁止挂写工具。
- AI 工具集里本来就没有 DDL 执行工具（只有 draft_ddl 草稿），组合无法越权。
- 每个工具执行仍走统一安全闸门（execute_tool 第一道拦截），组合只是收窄。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.ai.skills.registry import (
    get_skill,
    is_org_disabled,
    list_skills,
    register_custom,
    remove_skill,
    set_org_disabled,
    update_skill,
    validate_skill,
)
from app.ai.skills.skill import Skill
from app.ai.tools.registry import TOOL_SCHEMAS
from app.state import get_state

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


class SkillCreate(BaseModel):
    name: str
    description: str = ""
    system_prompt: str = ""
    tools: list[str]
    read_only: bool = False
    enabled: bool = True
    triggers: list[str] = []


class SkillPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    tools: list[str] | None = None
    read_only: bool | None = None
    enabled: bool | None = None
    triggers: list[str] | None = None


def _public(s: Skill) -> dict:
    d = s.to_dict()
    d.pop("script", None)
    return d


@router.get("")
async def list_all() -> dict:
    return {
        "skills": [_public(s) for s in list_skills()],
        "tools": [
            {"name": t["function"]["name"], "description": t["function"]["description"]}
            for t in TOOL_SCHEMAS
        ],
    }


@router.post("", status_code=201)
async def create_skill(body: SkillCreate) -> dict:
    errors = validate_skill(body.name, body.description, body.tools, body.triggers, body.read_only)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))
    skill = Skill(
        id="",
        name=body.name.strip(),
        description=body.description.strip(),
        system_prompt=body.system_prompt,
        tools=list(dict.fromkeys(body.tools)),
        read_only=body.read_only,
        enabled=body.enabled,
        triggers=[t.strip() for t in body.triggers if t.strip()],
        builtin=False,
    )
    saved = register_custom(skill)
    return _public(saved)


@router.put("/{skill_id}")
async def update(body: SkillPatch, skill_id: str, request: Request) -> dict:
    cur = get_skill(skill_id)
    if cur is None:
        raise HTTPException(status_code=404, detail="技能不存在")
    patch = body.model_dump(exclude_none=True)
    FLOOR = {"query", "refusal"}
    if skill_id in FLOOR and patch.get("enabled") is False:
        patch = {k: v for k, v in patch.items() if k != "enabled"}
        if not patch:
            return _public(cur)
    state = get_state()
    try:
        is_team = state.auth.is_team_mode()
    except Exception:
        is_team = False
    if is_team and patch.get("enabled") is True and cur.enabled is False and is_org_disabled(skill_id):
        user = getattr(request.state, "user", None)
        role = user.get("role") if isinstance(user, dict) else None
        if role == "member":
            raise HTTPException(status_code=403, detail="org policy disabled, member cannot re-enable")
    if "tools" in patch:
        errors = validate_skill(
            patch.get("name", cur.name),
            patch.get("description", cur.description),
            patch["tools"],
            patch.get("triggers", cur.triggers),
            patch.get("read_only", cur.read_only),
        )
        if errors:
            raise HTTPException(status_code=422, detail="; ".join(errors))
    before_enabled = cur.enabled
    updated = update_skill(skill_id, patch)
    if updated is None:
        raise HTTPException(status_code=404, detail="技能不存在")
    if "enabled" in patch and before_enabled != updated.enabled:
        try:
            user = getattr(request.state, "user", None)
            role = user.get("role") if isinstance(user, dict) else None
            is_admin = role == "admin" or not is_team
            if is_admin:
                set_org_disabled(skill_id, not updated.enabled)
        except Exception:
            pass
        try:
            state.audit.log(
                connection="settings",
                origin="api",
                tier="read",
                verdict="allow",
                status=f"skill {skill_id} enabled={updated.enabled}",
                sql=f"[settings] skill {skill_id} enabled={updated.enabled}",
                source="settings",
                skill_id=skill_id,
                enabled=updated.enabled,
            )
        except Exception:
            pass
    return _public(updated)


@router.delete("/{skill_id}")
async def delete(skill_id: str) -> dict:
    if not remove_skill(skill_id):
        raise HTTPException(status_code=403, detail="内置技能不可删除")
    return {"deleted": True}
