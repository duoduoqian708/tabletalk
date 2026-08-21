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
    query_max_rows: int | None = None
    pool_size: int | None = None


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
    runtime = state.runtime.update(body.model_dump(exclude_none=True))
    await state.pools.rebuild()
    return runtime.public()
