"""健康检查：存活 + 闸门 armed + 方言列表 + 网关状态。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from app.config import get_env, get_token
from app.core.dialects.registry import registry
from app.ai import gateway as gw
from app.state import get_state

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health")
async def health() -> dict:
    state = get_state()
    rs = state.runtime.get()
    cfg = rs.provider_config()
    effective = "mock" if gw.is_effective_mock(cfg) else cfg["provider"]
    default = rs._default_ai()
    return {
        "status": "ok",
        "gate": "armed",
        "gate_model_independent": True,
        "dialects": registry.names(),
        "ai_provider": default.provider,
        "ai_model": default.model or default.name,
        "ai_provider_name": default.name,
        "ai_effective_provider": effective,
        "ai_using_mock": effective == "mock",
        "connections": len(state.connections.list()),
    }


@router.get("/bootstrap")
async def bootstrap() -> dict:
    """引导端点（免鉴权）：向浏览器发放 sidecar token 与数据目录。

    仅本机可访问（默认绑 127.0.0.1），等价于本机读 data_dir/tabletalk.token。
    未来网络化部署需在此加门禁。
    """
    return {"token": get_token(), "dataDir": str(Path(get_env().data_dir))}
