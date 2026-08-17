"""知识库路由：构建（采样+图谱+向量）/ 审查视图 / 图谱 / 检索 / AI 自动注释 / 确认工作流。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.core.schema import get_schema, sample_values
from app.knowledge.annotator import annotate_domain, annotate_knowledge
from app.state import get_state

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


class AnnotateRequest(BaseModel):
    table: str | None = None
    column: str | None = None
    note: str
    title: str | None = None
    kind: str = "note"


class AutoAnnotateRequest(BaseModel):
    include_samples: bool | None = None


class TagNameRequest(BaseModel):
    name: str


class AssignTagsRequest(BaseModel):
    table: str
    tags: list[str]


class ConfirmRequest(BaseModel):
    table: str | None = None
    column: str | None = None


class RejectRequest(BaseModel):
    doc_id: str


class RejectCommentRequest(BaseModel):
    table: str
    column: str | None = None


def _have(conn_id: str) -> None:
    state = get_state()
    try:
        state.connections.get(conn_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{conn_id}/build")
async def build_index(conn_id: str) -> dict:
    _have(conn_id)
    state = get_state()
    schema = await get_schema(state, conn_id)
    rt = state.runtime.get()
    samples: dict[str, dict[str, list]] = {}
    if rt.kb_sample_rows > 0:
        for t in schema["tables"]:
            try:
                samples[t["name"]] = await sample_values(state, conn_id, t["name"], rt.kb_sample_rows)
            except Exception:
                samples[t["name"]] = {}
    stats = await state.knowledge.build(conn_id, schema, samples)
    return stats


@router.get("/{conn_id}/overview")
async def overview(conn_id: str) -> dict:
    _have(conn_id)
    state = get_state()
    if not state.knowledge.is_built(conn_id):
        schema = await get_schema(state, conn_id)
        rt = state.runtime.get()
        samples = {}
        if rt.kb_sample_rows > 0:
            for t in schema["tables"]:
                try:
                    samples[t["name"]] = await sample_values(state, conn_id, t["name"], rt.kb_sample_rows)
                except Exception:
                    samples[t["name"]] = {}
        await state.knowledge.build(conn_id, schema, samples)
    return state.knowledge.overview(conn_id)


@router.get("/{conn_id}/graph")
async def graph(conn_id: str) -> dict:
    _have(conn_id)
    state = get_state()
    return state.knowledge.graph(conn_id)


@router.get("/{conn_id}/retrieve")
async def retrieve(conn_id: str, q: str = "", table: str | None = None, k: int = 10) -> dict:
    _have(conn_id)
    state = get_state()
    docs = await state.knowledge.retrieve(conn_id, q, table, k)
    return {"count": len(docs), "docs": [d.to_dict() for d in docs]}


@router.get("/{conn_id}/docs")
async def list_docs(conn_id: str, table: str | None = None) -> dict:
    _have(conn_id)
    state = get_state()
    docs = state.knowledge.list_docs(conn_id, table)
    return {"count": len(docs), "docs": [d.to_dict() for d in docs]}


@router.put("/{conn_id}/docs", status_code=201)
async def annotate(conn_id: str, body: AnnotateRequest) -> dict:
    _have(conn_id)
    state = get_state()
    doc = state.knowledge.annotate(
        conn_id, body.table, body.column, body.note, title=body.title, kind=body.kind
    )
    return doc.to_dict()


@router.post("/{conn_id}/annotate")
async def auto_annotate(conn_id: str, body: AutoAnnotateRequest) -> dict:
    """AI 生成中文注释草案（draft），写入知识库待人工确认。"""
    _have(conn_id)
    state = get_state()
    return await annotate_knowledge(state, conn_id, body.include_samples)


@router.post("/{conn_id}/confirm")
async def confirm(conn_id: str, body: ConfirmRequest) -> dict:
    """人工确认草案为权威（确认后检索优先）。"""
    _have(conn_id)
    state = get_state()
    n = state.knowledge.confirm(conn_id, body.table, body.column)
    return {"confirmed": n}


@router.post("/{conn_id}/reject")
async def reject(conn_id: str, body: RejectRequest) -> dict:
    """拒绝/丢弃一条草案。"""
    _have(conn_id)
    state = get_state()
    ok = state.knowledge.reject(conn_id, body.doc_id)
    return {"rejected": ok}


@router.post("/{conn_id}/reject-comment")
async def reject_comment(conn_id: str, body: RejectCommentRequest) -> dict:
    """按表/列拒绝草案注释（审查页）。"""
    _have(conn_id)
    state = get_state()
    n = state.knowledge.reject_comment(conn_id, body.table, body.column)
    return {"rejected": n}


@router.get("/{conn_id}/tags")
async def tags(conn_id: str) -> dict:
    """标签库 + 表→标签 绑定。"""
    _have(conn_id)
    state = get_state()
    return state.knowledge.tags(conn_id)


@router.post("/{conn_id}/annotate-tags")
async def annotate_tags(conn_id: str) -> dict:
    """AI 生成逐表描述 + 领域标签（新标签 draft，约束复用已有标签）。"""
    _have(conn_id)
    state = get_state()
    return await annotate_domain(state, conn_id)


@router.post("/{conn_id}/tags/confirm")
async def confirm_tag(conn_id: str, body: TagNameRequest) -> dict:
    """人工确认标签 → 进入可路由标签库。"""
    _have(conn_id)
    state = get_state()
    return {"confirmed": state.knowledge.confirm_tag(conn_id, body.name)}


@router.post("/{conn_id}/tags/reject")
async def reject_tag(conn_id: str, body: TagNameRequest) -> dict:
    """拒绝标签：移除并解除绑定。"""
    _have(conn_id)
    state = get_state()
    return {"rejected": state.knowledge.reject_tag(conn_id, body.name)}


@router.post("/{conn_id}/tags/assign")
async def assign_tags(conn_id: str, body: AssignTagsRequest) -> dict:
    """手工调整表→标签 绑定。"""
    _have(conn_id)
    state = get_state()
    n = state.knowledge.assign_table_tags(conn_id, body.table, body.tags)
    return {"assigned": n}


@router.post("/{conn_id}/route")
async def route(conn_id: str, body: AssignTagsRequest) -> dict:
    """按标签路由候选表（标签→表 + FK 多跳）。供图谱可视化 / 调试 / AI 上下文。"""
    _have(conn_id)
    state = get_state()
    return state.knowledge.route_tables(conn_id, body.tags, hops=2)
