"""知识库路由：构建（采样+图谱+向量）/ 审查视图 / 图谱 / 检索 / AI 自动注释 / 确认工作流。"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.schema import get_schema, sample_values
from app.core.sensitive import filter_sensitive
from app.knowledge.annotator import annotate_domain, annotate_knowledge
from app.state import get_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


async def _kb_schema(state, conn_id: str, refresh: bool = False) -> dict:
    """取结构并应用连接级敏感名单（屏蔽表/列不进知识库文档/向量/图谱）。"""
    cfg = state.connections.get(conn_id)
    return filter_sensitive(await get_schema(state, conn_id, refresh=refresh), cfg.sensitive)


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


class TagCreateRequest(BaseModel):
    name: str
    description: str = ""


class TagUpdateRequest(BaseModel):
    name: str
    new_name: str | None = None
    description: str | None = None


class AssignTagsRequest(BaseModel):
    table: str
    tags: list[str]


class ConfirmRequest(BaseModel):
    table: str | None = None
    column: str | None = None


class RejectRequest(BaseModel):
    table: str
    column: str | None = None


class RejectCommentRequest(BaseModel):
    table: str
    column: str | None = None


class GraphEdgeRequest(BaseModel):
    from_table: str
    to_table: str
    kind: str = "user"
    from_col: str | None = None
    to_col: str | None = None
    weight: float | None = None


class GraphEdgeDelete(BaseModel):
    from_table: str
    to_table: str
    kind: str


class GraphExcludeRequest(BaseModel):
    table: str
    excluded: bool = True


class BuildRequest(BaseModel):
    include_samples: bool = False   # spec §3.8：默认不勾，零实例数据出网需显式授权
    trigger: str = "init"   # init | rebuild


def _have(conn_id: str) -> None:
    state = get_state()
    try:
        state.connections.get(conn_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{conn_id}/build")
async def build_index(conn_id: str, body: BuildRequest | None = None) -> dict:
    """启动后台构建任务（接入流程强制步骤）。返回后立即轮询 /build/progress。"""
    _have(conn_id)
    state = get_state()
    if state.build_jobs.is_running(conn_id):
        raise HTTPException(status_code=409, detail="构建已在运行")
    if body is None:
        body = BuildRequest()
    if body.trigger not in ("init", "rebuild"):
        raise HTTPException(status_code=422, detail="trigger 必须为 init|rebuild")
    # 确认留痕（spec §3.8）：用户点击"开始构建"即写审计，先于后台任务启动
    state.audit.log(
        connection=conn_id,
        origin="kb_build",
        tier="read",
        verdict="allow",
        status="confirmed",
        sql=f"-- kb build trigger={body.trigger} include_samples={body.include_samples}",
        source="manual",
        trigger=body.trigger,
        include_samples=body.include_samples,
    )
    include_samples = body.include_samples
    state.connections.set_kb_status(conn_id, "building")

    async def _run(report):
        report("发现结构", 5)
        schema = await _kb_schema(state, conn_id)
        report("发现结构", 10)
        rt = state.runtime.get()
        samples: dict[str, dict[str, list]] = {}
        if rt.kb_sample_rows > 0:
            n = max(1, len(schema["tables"]))
            for i, t in enumerate(schema["tables"]):
                report("抽样取值", 10 + 5 * i // n)
                try:
                    samples[t["name"]] = await sample_values(state, conn_id, t["name"], rt.kb_sample_rows)
                except Exception:
                    samples[t["name"]] = {}
        return await state.knowledge.build(
            conn_id, schema, samples, on_progress=report,
            include_samples=include_samples,
        )

    state.build_jobs.start(conn_id, _run)
    logger.info(
        "[kb.api] conn=%s 启动构建 trigger=%s include_samples=%s",
        conn_id, body.trigger, body.include_samples,
    )
    return {"job_id": conn_id, "kb_status": "building", "stage": "排队中"}


@router.get("/{conn_id}/build/progress")
async def build_progress(conn_id: str) -> dict:
    """轮询构建进度（500ms）：{stage, percent, done, error, kb_status}。"""
    _have(conn_id)
    state = get_state()
    cfg = state.connections.get(conn_id)
    p = state.build_jobs.progress(conn_id)
    if p is None:
        return {"stage": "idle", "percent": 0, "done": True, "error": None, "kb_status": cfg.kb_status}
    p["kb_status"] = cfg.kb_status
    return p


@router.get("/{conn_id}/build/events")
async def build_events(conn_id: str) -> StreamingResponse:
    """SSE 进度推送：构建开始时连接，进度变化实时推送，任务结束推送 done 后关闭。

    与轮询端点互补：前端发起构建后走这里拿实时进度，无需 500ms 轮询。
    """
    _have(conn_id)
    state = get_state()
    job = state.build_jobs.get(conn_id)

    async def _stream():
        # 任务不存在（未发起/已结束）→ 推一条当前状态后关闭
        if job is None:
            yield "data: " + json.dumps({"stage": "idle", "percent": 0, "done": True, "error": None}) + "\n\n"
            return
        while True:
            try:
                await job.event.wait()
            except asyncio.CancelledError:
                break
            job.event.clear()
            yield "data: " + json.dumps(dict(job.progress)) + "\n\n"
            if job.progress.get("done"):
                break

    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/{conn_id}/build/cancel")
async def build_cancel(conn_id: str) -> dict:
    """取消构建：清理半成品，kb_status 回 none。"""
    _have(conn_id)
    state = get_state()
    return {"cancelled": state.build_jobs.cancel(conn_id)}


@router.get("/{conn_id}/status")
async def kb_status(conn_id: str) -> dict:
    """状态机查询：{kb_status, kb_updated_at, synced_at, pending, building}。"""
    _have(conn_id)
    state = get_state()
    cfg = state.connections.get(conn_id)
    state.knowledge.ensure_loaded(conn_id)  # 重启后恢复内存态
    return {
        "kb_status": cfg.kb_status,
        "kb_updated_at": cfg.kb_updated_at,
        "synced_at": state.knowledge.synced_at(conn_id),
        "pending": state.knowledge.pending_counts(conn_id),
        "building": state.build_jobs.is_running(conn_id),
    }


@router.post("/{conn_id}/sync")
async def sync_kb(conn_id: str) -> dict:
    """手动增量同步：指纹对比 → 变化则抽样 + 增量构建（返回 diff 摘要）。"""
    _have(conn_id)
    state = get_state()
    cfg = state.connections.get(conn_id)
    if cfg.kb_status != "ready":
        raise HTTPException(status_code=409, detail={
            "code": "kb_not_ready", "message": "知识库未就绪，无法同步",
        })
    if state.build_jobs.is_running(conn_id):
        raise HTTPException(status_code=409, detail="构建/同步已在运行")
    schema = await _kb_schema(state, conn_id, refresh=True)  # 手动检查：强制最新结构
    if not state.knowledge.needs_sync(conn_id, schema):
        logger.debug("[kb.api] conn=%s 手动同步：结构无变化", conn_id)
        return {
            "changed": False, "tables_added": 0, "tables_removed": 0,
            "tables_changed": 0, "message": "结构无变化",
        }
    rt = state.runtime.get()
    samples: dict[str, dict[str, list]] = {}
    if rt.kb_sample_rows > 0:
        for t in schema["tables"]:
            try:
                samples[t["name"]] = await sample_values(state, conn_id, t["name"], rt.kb_sample_rows)
            except Exception as e:
                logger.warning("[kb.api] conn=%s 抽样失败 table=%s：%s", conn_id, t["name"], e)
                samples[t["name"]] = {}
    result = await state.knowledge.sync(conn_id, schema, samples)
    logger.info(
        "[kb.api] conn=%s 手动同步结果：changed=%s +%s表 -%s表 变更%s表",
        conn_id, result.get("changed"), result.get("tables_added", 0),
        result.get("tables_removed", 0), result.get("tables_changed", 0),
    )
    return result


@router.post("/{conn_id}/confirm-all")
async def confirm_all(conn_id: str) -> dict:
    """确认闸：一键确认全部草案文档 + draft 标签 → kb_status=ready（解锁数据源）。"""
    _have(conn_id)
    state = get_state()
    n = state.knowledge.confirm_all(conn_id)
    state.connections.set_kb_status(conn_id, "ready")
    return {**n, "kb_status": "ready"}


@router.get("/{conn_id}/overview")
async def overview(conn_id: str) -> dict:
    _have(conn_id)
    state = get_state()
    cfg = state.connections.get(conn_id)
    if not state.knowledge.is_built(conn_id):
        # 未构建：返回空结构 + kb_status，前端据此弹构建窗（不再隐式同步构建）
        return {
            "built": False, "kb_status": cfg.kb_status,
            "tables": [], "graph": {"edges": []},
            "tags": {"library": [], "tables": {}},
            "draft_count": 0, "tag_draft_count": 0, "sample_cols": 0,
            "embedding_provider": state.runtime.get().embedding_provider,
        }
    # 重启后工件存在但未加载进内存：恢复后再出 overview（否则表列表为空）
    state.knowledge.ensure_loaded(conn_id)
    await state.knowledge.reembed_if_needed(conn_id)  # 用户更换嵌入模型 → 向量重嵌
    return {**state.knowledge.overview(conn_id), "built": True, "kb_status": cfg.kb_status,
            "synced_at": state.knowledge.synced_at(conn_id)}


@router.get("/{conn_id}/graph")
async def graph(conn_id: str) -> dict:
    _have(conn_id)
    state = get_state()
    if not state.knowledge.is_built(conn_id):
        return {"built": False, "kb_status": state.connections.get(conn_id).kb_status, "edges": []}
    state.knowledge.ensure_loaded(conn_id)
    return {**state.knowledge.graph(conn_id), "built": True}


@router.post("/{conn_id}/graph/edges")
async def add_graph_edge(conn_id: str, body: GraphEdgeRequest) -> dict:
    """新增一条图谱边（kind=user 为用户手动连线）。持久化到知识库。"""
    _have(conn_id)
    state = get_state()
    if not state.knowledge.is_built(conn_id):
        raise HTTPException(status_code=409, detail="知识库未就绪")
    state.knowledge.ensure_loaded(conn_id)
    try:
        edge = state.knowledge.add_graph_edge(
            conn_id, body.from_table, body.to_table, body.kind,
            body.from_col, body.to_col, body.weight)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"edge": edge, "graph": state.knowledge.graph(conn_id)}


@router.delete("/{conn_id}/graph/edges")
async def remove_graph_edge(conn_id: str, body: GraphEdgeDelete) -> dict:
    """删除一条图谱边。删除结构/取值派生边时记入 tombstone，重建不复活。"""
    _have(conn_id)
    state = get_state()
    if not state.knowledge.is_built(conn_id):
        raise HTTPException(status_code=409, detail="知识库未就绪")
    state.knowledge.ensure_loaded(conn_id)
    removed = state.knowledge.remove_graph_edge(conn_id, body.from_table, body.to_table, body.kind)
    return {"removed": removed, "graph": state.knowledge.graph(conn_id)}


@router.post("/{conn_id}/graph/exclude")
async def exclude_table(conn_id: str, body: GraphExcludeRequest) -> dict:
    """将表移出/移回图谱视图（不影响审查页表列表）。"""
    _have(conn_id)
    state = get_state()
    if not state.knowledge.is_built(conn_id):
        raise HTTPException(status_code=409, detail="知识库未就绪")
    state.knowledge.ensure_loaded(conn_id)
    state.knowledge.set_table_excluded(conn_id, body.table, body.excluded)
    return {"excluded": state.knowledge.excluded_tables(conn_id)}


class GraphConfirmRequest(BaseModel):
    from_table: str | None = None   # None = 确认全部


@router.post("/{conn_id}/graph/confirm")
async def confirm_graph_drafts(conn_id: str, body: GraphConfirmRequest | None = None) -> dict:
    """确认 LLM 发现的 draft 边 → 写入正式图谱（from_table=None 则确认全部）。"""
    _have(conn_id)
    state = get_state()
    if not state.knowledge.is_built(conn_id):
        raise HTTPException(status_code=409, detail="知识库未就绪")
    state.knowledge.ensure_loaded(conn_id)
    ft = body.from_table if body else None
    added = state.knowledge.confirm_graph_edges(conn_id, from_table=ft)
    return {"confirmed": added, "llm_draft_edges": state.knowledge.llm_graph_edges(conn_id)}


@router.post("/{conn_id}/graph/reject")
async def reject_graph_drafts(conn_id: str, body: GraphConfirmRequest | None = None) -> dict:
    """拒绝 LLM 发现的 draft 边（从 draft 列表移除）。"""
    _have(conn_id)
    state = get_state()
    if not state.knowledge.is_built(conn_id):
        raise HTTPException(status_code=409, detail="知识库未就绪")
    state.knowledge.ensure_loaded(conn_id)
    ft = body.from_table if body else None
    removed = state.knowledge.reject_graph_edges(conn_id, from_table=ft)
    return {"rejected": removed, "llm_draft_edges": state.knowledge.llm_graph_edges(conn_id)}


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


@router.delete("/{conn_id}/docs/{doc_id}")
async def delete_doc(conn_id: str, doc_id: str) -> dict:
    """删除一条用户手写笔记（usr- 前缀）。"""
    _have(conn_id)
    state = get_state()
    n = state.knowledge.delete_user_doc(conn_id, doc_id)
    if not n:
        raise HTTPException(status_code=404, detail="文档不存在或不可删除")
    return {"deleted": True}


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
    """拒绝草案注释（按表/列撤下：AI 内容清空回 none）。"""
    _have(conn_id)
    state = get_state()
    n = state.knowledge.reject(conn_id, body.table, body.column)
    return {"rejected": n}


@router.post("/{conn_id}/reject-comment")
async def reject_comment(conn_id: str, body: RejectCommentRequest) -> dict:
    """拒绝草案注释（审查页逐列 ✕）。"""
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


@router.post("/{conn_id}/tags/create")
async def create_tag(conn_id: str, body: TagCreateRequest) -> dict:
    """人工新建标签（直接 confirmed，立即可路由）。"""
    _have(conn_id)
    state = get_state()
    ok = state.knowledge.create_tag(conn_id, body.name, body.description)
    if not ok:
        raise HTTPException(status_code=409, detail=f"标签 {body.name} 已存在或名称为空")
    return {"created": True}


@router.post("/{conn_id}/tags/update")
async def update_tag(conn_id: str, body: TagUpdateRequest) -> dict:
    """人工编辑标签（改名同步表绑定 / 改描述）。"""
    _have(conn_id)
    state = get_state()
    try:
        ok = state.knowledge.update_tag(conn_id, body.name, new_name=body.new_name,
                                        description=body.description)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail=f"标签 {body.name} 不存在")
    return {"updated": True}


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
