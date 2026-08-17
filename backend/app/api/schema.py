"""Schema 路由：schema 树 / 单表描述 / 表预览 / DDL 导出。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.core.schema import describe_table, export_ddl, get_schema, preview_table
from app.state import get_state

router = APIRouter(prefix="/api/v1/connections/{conn_id}/schema", tags=["schema"])


def _have(conn_id: str) -> None:
    state = get_state()
    try:
        state.connections.get(conn_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("")
async def schema_tree(conn_id: str, refresh: bool = False) -> dict:
    _have(conn_id)
    state = get_state()
    return await get_schema(state, conn_id, refresh=refresh)


@router.get("/{table}")
async def table_desc(conn_id: str, table: str) -> dict:
    _have(conn_id)
    state = get_state()
    try:
        return await describe_table(state, conn_id, table)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/{table}/preview")
async def table_preview(conn_id: str, table: str, limit: int = Query(100, ge=1, le=1000)) -> dict:
    _have(conn_id)
    state = get_state()
    return await preview_table(state, conn_id, table, limit=limit)


@router.get("/{table}/ddl")
async def table_ddl(conn_id: str, table: str) -> dict:
    _have(conn_id)
    state = get_state()
    try:
        return await export_ddl(state, conn_id, table)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
