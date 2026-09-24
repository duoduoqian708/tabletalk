"""连接 CRUD + 测试。统一配置模型：dialect 字段映射到方言注册表。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.state import get_state

router = APIRouter(prefix="/api/v1/connections", tags=["connections"])


class ConnectionCreate(BaseModel):
    name: str = ""
    dialect: str = "sqlite"
    host: str = ""
    port: int | None = None
    user: str = ""
    password: str = ""
    database: str = ""
    file: str = ""
    ssl: bool = False
    read_only: bool = False
    timeout: int = 10
    credential_ref: str | None = None
    sensitive: list[str | dict[str, Any]] = Field(default_factory=list)  # 敏感表/列名单（不进模型上下文与知识库）：字符串=旧 glob；dict{table,columns[]} 精确名
    session_vars: dict[str, Any] | None = None  # T8 连接级会话变量默认值（:current_tenant 等）


class TestDraftBody(ConnectionCreate):
    """编辑模式测试：密码留空 + saved_conn_id → 用已存密码填充（旧密码永不出网，前端只见 *** 占位）。"""
    saved_conn_id: str | None = None


class ConnectionUpdate(BaseModel):
    name: str | None = None
    dialect: str | None = None
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None
    file: str | None = None
    ssl: bool | None = None
    read_only: bool | None = None
    timeout: int | None = None
    credential_ref: str | None = None
    sensitive: list[str | dict[str, Any]] | None = None
    session_vars: dict[str, Any] | None = None


def _get_cfg(state, conn_id):
    try:
        return state.connections.get(conn_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("")
async def list_connections() -> list[dict]:
    state = get_state()
    return [state.connections.public(c) for c in state.connections.list()]


@router.post("", status_code=201)
async def create_connection(body: ConnectionCreate) -> dict:
    state = get_state()
    cfg = state.connections.create(body.model_dump())
    return state.connections.public(cfg)


@router.get("/{conn_id}")
async def get_connection(conn_id: str) -> dict:
    state = get_state()
    return state.connections.public(_get_cfg(state, conn_id))


@router.put("/{conn_id}")
async def update_connection(conn_id: str, body: ConnectionUpdate) -> dict:
    state = get_state()
    cfg = state.connections.update(conn_id, body.model_dump(exclude_none=True))
    return state.connections.public(cfg)


@router.delete("/{conn_id}")
async def delete_connection(conn_id: str) -> dict:
    state = get_state()
    state.connections.delete(conn_id)
    await state.pools.close(conn_id)
    return {"deleted": conn_id}


@router.post("/test-draft")
async def test_draft_connection(body: TestDraftBody) -> dict:
    """接入流程前置：测试连接配置（不落盘）。通过后才允许 POST /connections 保存。

    编辑模式（saved_conn_id 且密码留空）→ 用已存密码填充后测试；
    未知 saved_conn_id 按无密码测（不泄露存在性）。SQLite 无"库"概念 → 文件可读即可。
    """
    state = get_state()
    data = body.model_dump(exclude={"saved_conn_id"})
    if not data.get("password") and body.saved_conn_id:
        try:
            data["password"] = state.connections.get(body.saved_conn_id).password
        except KeyError:
            pass
    return await state.pools.test_draft(data)


@router.post("/{conn_id}/test")
async def test_connection(conn_id: str) -> dict:
    state = get_state()
    _get_cfg(state, conn_id)
    return await state.pools.test(conn_id)
