"""运行时设置路由：AI 网关 + 闸门参数。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.state import get_state

router = APIRouter(prefix="/api/v1", tags=["settings"])


class SettingsUpdate(BaseModel):
    # 新格式：多模型列表
    ai_models: list[dict[str, Any]] | None = None
    default_ai_model: str | None = None
    embedding_models: list[dict[str, Any]] | None = None
    default_embedding_model: str | None = None
    # 旧格式兼容（更新默认模型的对应字段）
    ai_provider: str | None = None
    ai_base_url: str | None = None
    ai_api_key: str | None = None
    ai_model: str | None = None
    ai_temperature: float | None = None
    ai_timeout: float | None = None
    embedding_provider: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_model: str | None = None
    # 通用
    gate_review_threshold: int | None = None
    gate_rules: dict[str, Any] | None = None
    kb_sample_rows: int | None = None
    kb_ai_annotation_samples: bool | None = None
    kb_build_reasoning_effort: str | None = None  # KB 阶段3 推理档位（off/low/medium/high）
    query_max_rows: int | None = None
    pool_size: int | None = None
    # 策略与隐私（A2/B4 修复：Pydantic 缺字段导致 PUT 静默丢弃）
    policy: dict[str, Any] | None = None
    privacy_mode: str | None = None


@router.get("/settings")
async def get_settings() -> dict:
    state = get_state()
    return state.runtime.get().public()


@router.put("/settings")
async def update_settings(body: SettingsUpdate, request: Request) -> dict:
    # E3 统一出口：团队模式下仅 admin 可改组织级模型配置
    try:
        from app.state import get_state as _gs
        if _gs().auth.is_team_mode() and body.ai_models is not None:
            user = getattr(request.state, "user", None) if hasattr(request, "state") else None
            role = user.get("role") if isinstance(user, dict) else None
            if role and role != "admin":
                from fastapi import HTTPException
                raise HTTPException(status_code=403, detail="team mode: only admin can update ai_models")
    except Exception as e:
        if e.__class__.__name__ == "HTTPException":
            raise
        pass
    state = get_state()
    # 记录变更前快照，用于审计
    before_mode = state.runtime.get().privacy_mode
    before_policy_ver = getattr(state.runtime.get().policy, "version", 0)
    before_gate = dict(state.runtime.get().gate_rules or {})
    runtime = state.runtime.update(body.model_dump(exclude_none=True))
    await state.pools.rebuild()
    # B4: 档位/策略/规则切换写审计（可追溯——安全页要能回答"规则何时改的、改了什么"）
    try:
        import json as _json

        if body.privacy_mode is not None and body.privacy_mode != before_mode:
            state.audit.log(connection="settings", origin="api", tier="read", verdict="allow", status=f"privacy_mode {before_mode}->{body.privacy_mode}", sql=f"[settings] privacy_mode={body.privacy_mode}", source="settings")
        if body.policy is not None:
            state.audit.log(connection="settings", origin="api", tier="read", verdict="allow", status=f"policy v{before_policy_ver}->v{runtime.policy.version}", sql=f"[settings] policy updated", source="settings")
        if body.gate_rules is not None and (runtime.gate_rules or {}) != before_gate:
            _cur = _json.dumps(runtime.gate_rules or {}, ensure_ascii=False, sort_keys=True)
            state.audit.log(connection="settings", origin="api", tier="read", verdict="allow",
                            status="gate_rules 更新", sql=f"[settings] gate_rules={_cur}", source="settings")
    except Exception:
        pass
    return runtime.public()
