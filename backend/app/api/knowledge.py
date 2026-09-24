"""知识库路由：构建（采样+图谱+向量）/ 审查视图 / 图谱 / 检索 / AI 自动注释 / 确认工作流。"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.schema import get_schema, sample_values
from app.knowledge.annotator import annotate_domain, annotate_knowledge
from app.knowledge.jobs import JobBusyError
from app.state import get_state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


async def _kb_schema(state, conn_id: str, refresh: bool = False) -> dict:
    """取结构（敏感名单过滤已随功能下线移除，结构全量进知识库）。"""
    return await get_schema(state, conn_id, refresh=refresh)



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


class TableNameRequest(BaseModel):
    table: str


class TagCreateRequest(BaseModel):
    name: str
    description: str = ""
    color: str = ""  # 空 → 前端回退哈希色板


class TagUpdateRequest(BaseModel):
    name: str
    new_name: str | None = None
    description: str | None = None
    color: str | None = None  # 空串=清色回退哈希；None=不改


class AssignTagsRequest(BaseModel):
    table: str
    tags: list[str]


class ConfirmRequest(BaseModel):
    table: str | None = None
    column: str | None = None
    table_only: bool = False  # true = 只裁决表级提案，不动列提案（审核页表描述 radio）


class RejectRequest(BaseModel):
    table: str
    column: str | None = None


class RejectCommentRequest(BaseModel):
    table: str
    column: str | None = None


class TableEditColumn(BaseModel):
    name: str
    comment: str | None = None
    values: str | None = None
    example: str | None = None


class TableEditRequest(BaseModel):
    """详情面板两块编辑（只写知识字段；schema 镜像/type 不可改）。"""
    table: str
    table_comment: str | None = None
    column_comments: list[TableEditColumn] | None = None
    vector_text: str | None = None   # 向量化片段覆盖；'' 清空回落合成


class GraphEdgeRequest(BaseModel):
    from_table: str
    to_table: str
    source: str = "user"
    from_col: str | None = None
    to_col: str | None = None
    weight: float | None = None
    cardinality: str = "n:1"   # n:1 | 1:1 | 1:N | N:M（默认 n:1）
    guard: str | None = None   # 多态关联守卫谓词（如 "X.type = 1"），普通关联省略
    cols: list[list[str]] | None = None   # 复合边完整列对 [[from,to],...]；缺省用 from_col/to_col 单列


class GraphEdgeDelete(BaseModel):
    from_table: str
    to_table: str
    source: str


class GraphExcludeRequest(BaseModel):
    table: str
    excluded: bool = True


class GraphLayoutRequest(BaseModel):
    """2D 图布局坐标快照（表名 → {x,y}），透传存入各表 payload.layout。"""
    layout: dict[str, dict]


class BuildRequest(BaseModel):
    include_samples: bool = False   # spec §3.8：默认不勾，零实例数据出网需显式授权
    trigger: str = "init"   # init | rebuild
    annotate_mode: str = "diff"   # diff=只提案变化表（重构默认）| full=全表重新注释
    tag_mode: str = "keep"   # keep=沿用锚点 | anchor=锚点+允许改名合并 | fresh=标签从零


def _have(conn_id: str) -> None:
    state = get_state()
    try:
        state.connections.get(conn_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


def _ai_usable(m: object) -> bool:
    """模型真实可用判定：provider 非空，且 mock 之外必须有 base_url（cloud+缺 key 视为不可用）。"""
    provider = (getattr(m, "provider", "") or "").strip()
    if not provider:
        return False
    if provider == "mock":
        return True
    return bool((getattr(m, "base_url", "") or "").strip())


def _require_ai_for_build(state) -> None:
    """构建强制前置（用户规则）：对话 + 嵌入两类模型都必须配置且可用，缺任一 → 400 引导去设置。

    测试环境（mock provider）豁免：conftest 只注入 mock 对话模型、无嵌入模型，
    闸门若拦会让 800+ 既有测试全 400；mock 无真实出网，无强制前置的意义。
    """
    rt = state.runtime.get()
    chat = rt._default_ai()
    emb = rt._default_embedding()
    if (chat is not None and (chat.provider or "") == "mock") or (
        emb is not None and (emb.provider or "") == "mock"
    ):
        return
    if not chat or not _ai_usable(chat):
        raise HTTPException(
            status_code=400,
            detail="知识库构建需要可用的对话大模型：请到 系统设置 → 大模型接入 → 文本推理模型 配置（供应商 + Base URL + 模型）后再试",
        )
    if not emb or not _ai_usable(emb):
        raise HTTPException(
            status_code=400,
            detail="知识库构建需要可用的嵌入模型：请到 系统设置 → 大模型接入 → 向量嵌入模型 配置（供应商 + Base URL + 模型）后再试",
        )


@router.post("/{conn_id}/build")
async def build_index(conn_id: str, body: BuildRequest | None = None) -> dict:
    """启动后台构建任务（接入流程强制步骤）。返回后立即轮询 /build/progress。"""
    _have(conn_id)
    state = get_state()
    if body is None:
        body = BuildRequest()
    if body.annotate_mode not in ("diff", "full"):
        raise HTTPException(status_code=422, detail="annotate_mode 须为 diff|full")
    if body.tag_mode not in ("keep", "anchor", "fresh"):
        raise HTTPException(status_code=422, detail="tag_mode 须为 keep|anchor|fresh")
    # 构建强制前置：对话 + 嵌入两类模型都必须真实可用（未配置/假配置 → 拒绝并引导去设置）
    _require_ai_for_build(state)
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
    # P0-D：先记录原状态再置 building——JobBusyError 时回退原状态而非硬编码 none
    # （此前：ready 库被 409 后变"未构建"，apply_sync_result_status 只在 ready 流转，
    #  之后永不恢复 → AI 断供直到手动重建）
    prev_status = state.connections.get(conn_id).kb_status
    state.connections.set_kb_status(conn_id, "building")
    # 互斥（D1）：start() 同步校验占坑（无 await，检查+启动原子），
    # 前台操作（sync/confirm/discard）在跑时拒绝启动
    try:
        state.build_jobs.start(conn_id, _make_build_fn(state, conn_id, body))
    except JobBusyError:
        state.connections.set_kb_status(conn_id, prev_status or "none")
        raise HTTPException(status_code=409, detail="构建已在运行")
    logger.info(
        "[kb.api] conn=%s 启动构建 trigger=%s include_samples=%s",
        conn_id, body.trigger, body.include_samples,
    )
    return {"job_id": conn_id, "kb_status": "building", "stage": "排队中"}


def _make_build_fn(state, conn_id: str, body: BuildRequest):
    """构造 build 闭包（结构发现 + 授权抽样 + 委托 facade.build）。

    抽样范围随 annotate_mode 收敛：diff 重构只抽变化表（先 diff 后采样，省 DB 压力）。
    """
    include_samples = body.include_samples

    async def _run(report):
        import time as _time
        _t0 = _time.monotonic()
        report("发现结构", 5)
        schema = await _kb_schema(state, conn_id, refresh=True)  # 构建必须用最新结构（30s 缓存会让刚改的 DDL 建旧库）
        logger.info("[kb.api] conn=%s 发现结构耗时 %.2fs（含串行 list_columns+count_rows×%d）",
                    conn_id, _time.monotonic() - _t0, len(schema.get("tables", [])))
        report("发现结构", 10)
        rt = state.runtime.get()
        # 严格零采样：仅用户显式勾选 include_samples 才抽取值（不授权 → 不抽不落盘不发）
        samples: dict[str, dict[str, list]] = {}
        sample_tables: list[str] | None = None
        if include_samples and body.annotate_mode == "diff":
            # diff 模式：只抽变化表样本（rename 前后表名都抽，宁多勿漏；粗算失败退回全表）
            try:
                kb = state.knowledge
                kb.ensure_loaded(conn_id)
                old = kb.semantic_store._schema.get(conn_id) or {}
                if old:
                    touched, diff = kb.build_service.compute_touched(old, schema)
                    renames = kb.build_service.match_renames(
                        old, schema, set(diff["removed_tables"]), set(diff["added_tables"]))
                    touched |= {r["from"] for r in renames} | {r["to"] for r in renames}
                    sample_tables = sorted(touched) if touched else []
            except Exception as e:  # noqa: BLE001 - 粗算失败退回全表采样
                logger.warning("[kb.api] conn=%s diff 采样范围粗算失败，退回全表：%s", conn_id, e)
                sample_tables = None
        if include_samples and rt.kb_sample_rows > 0:
            tables_to_sample = [t for t in schema["tables"] if sample_tables is None or t["name"] in sample_tables]
            _t1 = _time.monotonic()
            n = max(1, len(tables_to_sample))
            for i, t in enumerate(tables_to_sample):
                report("抽样取值", 10 + 5 * i // n)
                try:
                    samples[t["name"]] = await sample_values(state, conn_id, t["name"], rt.kb_sample_rows)
                except Exception:
                    samples[t["name"]] = {}
            logger.info("[kb.api] conn=%s 授权抽样耗时 %.2fs（逐表串行×%d）",
                        conn_id, _time.monotonic() - _t1, len(tables_to_sample))
        _t2 = _time.monotonic()
        result = await state.knowledge.build(
            conn_id, schema, samples, on_progress=report,
            include_samples=include_samples,
            annotate_mode=body.annotate_mode, tag_mode=body.tag_mode,
        )
        logger.info("[kb.api] conn=%s build()耗时 %.2fs（总链路 %.2fs）",
                    conn_id, _time.monotonic() - _t2, _time.monotonic() - _t0)
        return result

    return _run


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
async def build_events(conn_id: str, request: Request) -> StreamingResponse:
    """SSE 进度推送：构建开始时连接，进度变化实时推送，任务结束推送 done 后关闭。

    与轮询端点互补：前端发起构建后走这里拿实时进度，无需 500ms 轮询。
    生命周期全程落日志（观测）：订阅/关闭原因/帧数——断线排查的核心证据源。
    """
    _have(conn_id)
    state = get_state()
    job = state.build_jobs.get(conn_id)
    ip = request.client.host if request.client else "?"

    async def _stream():
        # 任务不存在（未发起/已结束）→ 推一条当前状态后关闭
        if job is None:
            logger.info("[kb.events] conn=%s ip=%s SSE 订阅即空（任务不存在/已结束）→ idle 帧", conn_id, ip)
            yield "data: " + json.dumps({"stage": "idle", "percent": 0, "done": True, "error": None}) + "\n\n"
            return
        frames, pings, reason = 0, 0, "done"
        logger.info("[kb.events] conn=%s ip=%s SSE 订阅建立（构建中）", conn_id, ip)
        try:
            # 先发当前状态、再等变更：订阅时任务已结束也能立刻拿到终态帧，
            # 避免「事件已被早先 clear 吞掉 done 更新」的竞态（构建极快/并行时更容易触发）
            while True:
                cur = dict(job.progress)
                yield "data: " + json.dumps(cur) + "\n\n"
                frames += 1
                if cur.get("done"):
                    break
                try:
                    # 心跳：LLM 思考等长静默期（单表最长 180s）无真实帧 → 5s 推一次 SSE 注释帧，
                    # 防浏览器/代理按空闲连接掐断（前端 readBuildEvents 对非 "data: " 行自动忽略）。
                    # 2026-09 从 15s 收紧：实测 Tailscale 代理/手机 NAT ~7s 静默即断（15s 心跳来不及发）
                    await asyncio.wait_for(job.event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pings += 1
                    yield ": ping\n\n"
                    continue
                except asyncio.CancelledError:
                    reason = "stream_cancelled"
                    break
                job.event.clear()
        except GeneratorExit:
            # 客户端断开（fetch 中止/页面关闭/网络切换/代理掐断）。
            # 关键观测点：进度通道死亡 ≠ 构建失败——后端任务继续跑，前端会降级轮询。
            reason = "client_disconnect"
            raise
        finally:
            logger.info("[kb.events] conn=%s ip=%s SSE 关闭 reason=%s frames=%s pings=%s",
                        conn_id, ip, reason, frames, pings)

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
    """状态机查询：{kb_status, kb_updated_at, synced_at, pending, building, needs_rebuild}。

    needs_rebuild（P0-E）：注册表说 ready/pending 但工件已作废（旧版本按空处理）——
    前端据此弹重建引导，替代此前「内存空 KB + 标 ready」的静默不一致（AI 拿空上下文）。
    """
    _have(conn_id)
    state = get_state()
    cfg = state.connections.get(conn_id)
    state.knowledge.ensure_loaded(conn_id)  # 重启后恢复内存态
    needs_rebuild = cfg.kb_status != "none" and state.knowledge.is_voided(conn_id)
    if needs_rebuild:
        # 治愈注册表（工件已作废：状态机对齐存储现实，AI 门禁/前端门禁随即正确引导）
        state.connections.set_kb_status(conn_id, "none")
        logger.warning("[kb.api] conn=%s 工件版本已作废，kb_status %s → none（引导重建）",
                       conn_id, cfg.kb_status)
    return {
        "kb_status": "none" if needs_rebuild else cfg.kb_status,
        "kb_updated_at": cfg.kb_updated_at,
        "synced_at": state.knowledge.synced_at(conn_id),
        "pending": state.knowledge.pending_counts(conn_id),
        "building": state.build_jobs.is_running(conn_id),
        "needs_rebuild": needs_rebuild,
    }


@router.post("/{conn_id}/sync")
async def sync_kb(conn_id: str) -> dict:
    """手动增量同步（任务化）：指纹快速检查 → 无变化同步返回；有变化启动 sync job。

    返回 {changed: false}（无变化）或 {changed: true, job_id}（前端接 /build/progress
    或 /build/events 看进度，与全量构建同一通道）。
    job 收尾：有提案 → pending_review（审核入口出现）；无提案 → 保持 ready；失败 → 保持 ready。
    """
    _have(conn_id)
    state = get_state()
    cfg = state.connections.get(conn_id)
    if cfg.kb_status != "ready":
        raise HTTPException(status_code=409, detail={
            "code": "kb_not_ready", "message": "知识库未就绪，无法同步",
        })
    # 快速检查（互斥占坑先于一切 await）
    try:
        state.build_jobs.begin_op(conn_id, "sync")
    except JobBusyError:
        raise HTTPException(status_code=409, detail="构建/同步已在运行")
    try:
        schema = await _kb_schema(state, conn_id, refresh=True)  # 手动检查：强制最新结构
        if not state.knowledge.needs_sync(conn_id, schema):
            logger.debug("[kb.api] conn=%s 手动同步：结构无变化", conn_id)
            return {
                "changed": False, "tables_added": 0, "tables_removed": 0,
                "tables_changed": 0, "message": "结构无变化",
            }
    finally:
        state.build_jobs.end_op(conn_id)
    # 有变化 → 启动 sync job（抽样/增量构建/状态流转全在 job 内）
    try:
        job = state.build_jobs.start(conn_id, _make_sync_fn(state, conn_id), kind="sync")
    except JobBusyError:
        raise HTTPException(status_code=409, detail="构建/同步已在运行")
    logger.info("[kb.api] conn=%s 启动增量同步 job", conn_id)
    return {"changed": True, "job_id": job.conn_id, "kb_status": cfg.kb_status, "stage": "排队中"}


def _make_sync_fn(state, conn_id: str):
    """构造 sync job 闭包：结构发现 → touched 抽样（授权时）→ 委托 facade.sync → 留痕。"""

    async def _run(report):
        import time as _time
        _t0 = _time.monotonic()
        report("发现结构", 10)
        schema = await _kb_schema(state, conn_id, refresh=True)
        rt = state.runtime.get()
        samples: dict[str, dict[str, list]] = {}
        # 严格零采样同构：只在授权时抽取，且只抽 touched 表（先 diff 再采样）
        if rt.kb_ai_annotation_samples and rt.kb_sample_rows > 0:
            from app.knowledge.jobs import _touched_for_sync
            touched = _touched_for_sync(state, conn_id, schema)
            n = max(1, len(touched))
            done = 0
            for t in schema["tables"]:
                if t["name"] not in touched:
                    continue
                report("抽样取值", 10 + 10 * done // n)
                try:
                    samples[t["name"]] = await sample_values(state, conn_id, t["name"], rt.kb_sample_rows)
                except Exception as e:
                    logger.warning("[kb.api] conn=%s 抽样失败 table=%s：%s", conn_id, t["name"], e)
                    samples[t["name"]] = {}
                done += 1
        report("增量构建", 30)
        result = await state.knowledge.sync(conn_id, schema, samples, on_progress=report)
        # 增量同步留痕（与 build/confirm/discard 同源 origin=kb_build）：历史抽屉可查每次变更
        if result.get("changed"):
            state.audit.log(
                connection=conn_id,
                origin="kb_build",
                tier="read",
                verdict="allow",
                status="synced",
                sql=(
                    "-- kb sync "
                    f"+{result.get('tables_added', 0)}表 "
                    f"-{result.get('tables_removed', 0)}表 "
                    f"变更{result.get('tables_changed', 0)}表"
                ),
                source="manual",
                added_tables=result.get("tables_added", 0),
                removed_tables=result.get("tables_removed", 0),
                changed_tables=result.get("tables_changed", 0),
                cleared_tags=result.get("cleared_tags", []),
            )
        logger.info(
            "[kb.api] conn=%s 手动同步结果：changed=%s +%s表 -%s表 变更%s表（总耗时 %.2fs）",
            conn_id, result.get("changed"), result.get("tables_added", 0),
            result.get("tables_removed", 0), result.get("tables_changed", 0),
            _time.monotonic() - _t0,
        )
        return result

    return _run


@router.post("/{conn_id}/confirm-all")
async def confirm_all(conn_id: str) -> dict:
    """确认闸（版本启用）：一键确认全部草案 + 版本启用流程（归档旧版本 → 全量写向量 → 版本+1）。

    请求同步等待：全部向量写入、旧版本归档/清理完成后才返回——前端以等待遮罩感知。
    """
    _have(conn_id)
    state = get_state()
    # 互斥（D1）：构建/同步在跑时禁止确认（防半程草案被确认 + 状态回退）
    try:
        state.build_jobs.begin_op(conn_id, "confirm")
    except JobBusyError:
        raise HTTPException(status_code=409, detail="构建/同步已在运行")
    try:
        n = await state.knowledge.confirm_all(conn_id)
        state.connections.set_kb_status(conn_id, "ready")
    finally:
        state.build_jobs.end_op(conn_id)
    # 留痕（与 build/discard 同源 origin=kb_build）：历史记录可看到「确认启用」时间点
    state.audit.log(
        connection=conn_id,
        origin="kb_build",
        tier="read",
        verdict="allow",
        status="confirmed",
        sql=f"-- kb confirm_all docs={n.get('docs', 0)} tags={n.get('tags', 0)} edges={n.get('edges', 0)}"
            f" archived={n.get('archived', 0)} version={n.get('version', 0)}",
        source="manual",
    )
    return {**n, "kb_status": "ready"}


@router.post("/{conn_id}/discard")
async def discard_kb_drafts(conn_id: str) -> dict:
    """放弃本轮全部草案（撤草案保历史）：draft 注释/标签/LLM 边全撤，confirmed 不动。

    状态流转：存在任何 confirmed 内容 → ready（旧知识继续可用）；
    全库无 confirmed 内容 → none（未构建态，重新引导构建）。
    审计留痕与构建发起对称：origin=kb_build / status=discarded / source=manual。
    """
    _have(conn_id)
    state = get_state()
    # 互斥（D1）：构建/同步在跑时禁止放弃（防与草案写入 last-writer-wins 互踩）
    try:
        state.build_jobs.begin_op(conn_id, "discard")
    except JobBusyError:
        raise HTTPException(status_code=409, detail="构建/同步已在运行")
    try:
        state.knowledge.ensure_loaded(conn_id)
        discarded = await state.knowledge.discard_drafts(conn_id)
        new_status = "ready" if state.knowledge.has_confirmed_content(conn_id) else "none"
        state.connections.set_kb_status(conn_id, new_status)
    finally:
        state.build_jobs.end_op(conn_id)
    state.audit.log(
        connection=conn_id,
        origin="kb_build",
        tier="read",
        verdict="allow",
        status="discarded",
        sql=f"-- kb discard columns={discarded['columns']} tables={discarded['tables']}"
            f" tags={discarded['tags']} edges={discarded['edges']}",
        source="manual",
        discarded=discarded,
    )
    return {"discarded": discarded, "kb_status": new_status}


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
            "synced_at": state.knowledge.synced_at(conn_id),
            "version": state.knowledge.current_version(conn_id),
            "round": state.knowledge.round_meta(conn_id)}


# ═══ 审核重构（2026-09）：本轮对比区端点 ═══

@router.get("/{conn_id}/round/table/{table}")
async def round_table_baseline(conn_id: str, table: str) -> dict:
    """单表旧版知识（审核页对比层"旧"侧）：{comment, ddl, columns}。无快照 → has_baseline=false。"""
    _have(conn_id)
    state = get_state()
    state.knowledge.ensure_loaded(conn_id)
    base = state.knowledge.round_table_baseline(conn_id, table)
    return {"table": table, "has_baseline": bool(base), "baseline": base or {}}


class BatchReviewRequest(BaseModel):
    tables: list[str]
    action: str  # confirm | reject


# 后台重嵌任务引用保持（asyncio 官方建议：create_task 结果不留引用可能被 GC 中途回收）
_BG_REEMBED_TASKS: set = set()


@router.post("/{conn_id}/tables/batch-review")
async def batch_review(conn_id: str, body: BatchReviewRequest) -> dict:
    """批量裁决（审核页筛选级快审）：一次请求提升/撤销多张表的全部提案，
    合并受影响表集只做一次重嵌 + 一次落盘（避免几百次单表请求的重复开销）。

    启用收尾（2026-09 审核重构）：裁决后若待审全清（注释 + 标签 + draft 边），
    执行版本启用收尾（清 diff 基线/对比区 → 版本+1 → kb_status=ready）并审计留痕。
    """
    _have(conn_id)
    state = get_state()
    if body.action not in ("confirm", "reject"):
        raise HTTPException(status_code=422, detail="action 须为 confirm|reject")
    try:
        state.build_jobs.begin_op(conn_id, "confirm")
    except JobBusyError:
        raise HTTPException(status_code=409, detail="构建/同步已在运行")
    try:
        state.knowledge.ensure_loaded(conn_id)
        facade = state.knowledge
        sem = facade.semantic_store
        applied = 0
        affected: list[str] = []
        # P2-15：确认前归档当前生效字段（与 confirm-all 对齐）——此前 batch-review 路径
        # 不归档，经审核镜头裁决的字段变更在「版本回溯」里永远查不到
        if body.action == "confirm" and body.tables:
            try:
                facade.build_service._archive_current_fields(conn_id, facade._storage)
            except Exception as e:
                logger.warning("[kb.api] conn=%s 字段归档失败（不阻塞裁决）：%s", conn_id, e)
        for t in dict.fromkeys(body.tables):
            tk = sem._tables.get(conn_id, {}).get(t)
            if tk is None:
                continue
            if body.action == "confirm":
                applied += await sem.confirm(conn_id, t, save_conn_fn=None, reembed_fn=None)
            else:
                applied += await sem.reject(conn_id, t, save_conn_fn=None, reembed_fn=None)
            affected.append(t)
        if affected:
            facade._save_conn(conn_id)
            if body.action == "confirm":
                # 向量重嵌改后台任务（2026-09 修复）：45 表 × embedding 调用 = 20~90s，
                # 同步 await 会拖住 HTTP 响应 → 手机端 Tailscale 代理/浏览器超时断连 →
                # version mask 卡死 + 按钮不可用。改火后不管：HTTP 秒回，重嵌在事件循环后台跑。
                # 进程重启丢失未完成重嵌 → reembed_if_needed 在下次 AI 对话/同步自动补偿（已有机制）。
                import asyncio as _asyncio  # noqa: PLC0415
                _t = _asyncio.create_task(facade._reembed_tables(conn_id, affected))
                _BG_REEMBED_TASKS.add(_t)
                _t.add_done_callback(_BG_REEMBED_TASKS.discard)
        kb_status = "pending_review"
    finally:
        state.build_jobs.end_op(conn_id)
    return {"applied": applied, "tables": affected,
            "pending": sem.pending_counts(conn_id), "kb_status": kb_status,
            "version": facade.current_version(conn_id)}


def _maybe_finalize(state, conn_id: str) -> tuple[str, int]:
    """审核收尾判定：待审全清（注释提案 + draft 标签 + draft 边均为零）→ 版本启用收尾。

    对齐 confirm-all 的收尾子集（不含全表重嵌——批量 confirm 已按受影响表重嵌，
    标签/边不影响向量文本）。返回 (kb_status, version)。
    """
    facade = state.knowledge
    sem = facade.semantic_store
    pending = sem.pending_counts(conn_id)
    draft_edges = len(facade.graph_store._llm_graph_edges.get(conn_id, []) or [])
    if pending["draft_docs"] > 0 or pending["draft_tags"] > 0 or draft_edges > 0:
        return "pending_review", facade.current_version(conn_id)
    # P1-2：审核收尾 = 确认生效，未 pin 的删除建议边（红边）在此移除
    facade.apply_diff_removals(conn_id)
    facade.graph_store.clear_diff_base(conn_id)
    sem.clear_round(conn_id)  # 审核完成 → 清本轮对比区
    facade._storage(conn_id).bump_kb_version()
    # P2-15：收尾必须落盘——此前只 bump 版本不 save，重启后已收尾的库又显示陈旧本轮对比区
    facade._save_conn(conn_id)
    state.connections.set_kb_status(conn_id, "ready")
    version = facade.current_version(conn_id)
    state.audit.log(
        connection=conn_id, origin="kb_build", tier="read", verdict="allow",
        status="confirmed", source="manual",
        sql=f"-- kb review applied (batch-review) version={version}",
    )
    logger.info("[kb.review] conn=%s 待审全清 → 版本启用收尾 version=%s", conn_id, version)
    return "ready", version


class TagApplyRoundRequest(BaseModel):
    keep_old: list[str] = []
    adopt_new: list[str] = []


@router.post("/{conn_id}/tags/apply-round")
async def apply_round_tags(conn_id: str, body: TagApplyRoundRequest) -> dict:
    """标签版本制应用（审核页两栏混选）：应用后标签库 = 勾选结果（新版勾选 ∪ 旧版勾选），其余淘汰。"""
    _have(conn_id)
    state = get_state()
    state.knowledge.ensure_loaded(conn_id)
    sem = state.knowledge.semantic_store
    if not sem.get_round(conn_id).get("tags_new"):
        raise HTTPException(status_code=409, detail="本轮无标签全集（增量构建无全集对比）")
    r = sem.apply_round_tags(conn_id, body.keep_old, body.adopt_new, save_conn_fn=state.knowledge._save_conn)
    sem.set_round(conn_id, tags_new=[])  # 标签应用完成 → 只清标签全集（注释/边对比区不动）
    return {**r, "tags": sem.tags(conn_id)}


@router.post("/{conn_id}/review/finalize")
async def review_finalize(conn_id: str) -> dict:
    """审核收尾（前端应用选择流程最后一步调用）：待审全清 → 版本启用（ready）；
    尚有未审项 → 保持 pending_review（前端继续审核）。"""
    _have(conn_id)
    state = get_state()
    try:
        state.build_jobs.begin_op(conn_id, "confirm")
    except JobBusyError:
        raise HTTPException(status_code=409, detail="构建/同步已在运行")
    try:
        state.knowledge.ensure_loaded(conn_id)
        kb_status, version = _maybe_finalize(state, conn_id)
    finally:
        state.build_jobs.end_op(conn_id)
    return {"kb_status": kb_status, "version": version,
            "pending": state.knowledge.semantic_store.pending_counts(conn_id)}


@router.get("/{conn_id}/field-history")
async def field_history(conn_id: str, table: str, column: str) -> dict:
    """字段历史版本（版本制：确认时归档，供「版本回溯」复用旧值）。"""
    _have(conn_id)
    state = get_state()
    return {"items": state.knowledge.field_history(conn_id, table, column)}


class FieldHistoryApplyRequest(BaseModel):
    table: str
    column: str
    history_id: int


@router.post("/{conn_id}/field-history/apply")
async def apply_field_history(conn_id: str, body: FieldHistoryApplyRequest) -> dict:
    """用历史版本覆盖当前字段（comment/values/example，状态不变）。"""
    _have(conn_id)
    state = get_state()
    ok = state.knowledge.apply_field_history(conn_id, body.table, body.column, body.history_id)
    if not ok:
        raise HTTPException(status_code=404, detail="历史记录不存在或字段不匹配")
    return {"applied": True}


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
    """新增一条图谱边（source=user 为用户手动连线）。持久化到知识库。"""
    _have(conn_id)
    state = get_state()
    if not state.knowledge.is_built(conn_id):
        raise HTTPException(status_code=409, detail="知识库未就绪")
    state.knowledge.ensure_loaded(conn_id)
    try:
        edge = state.knowledge.add_graph_edge(
            conn_id, body.from_table, body.to_table, body.source,
            body.from_col, body.to_col, body.weight, body.cardinality,
            guard=body.guard, cols=body.cols)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"edge": edge, "graph": state.knowledge.graph(conn_id)}


@router.delete("/{conn_id}/graph/edges")
async def remove_graph_edge(conn_id: str, body: GraphEdgeDelete) -> dict:
    """删除一条图谱边（当前版本生效；确定性来源的边在下次重建时会重新生成）。"""
    _have(conn_id)
    state = get_state()
    if not state.knowledge.is_built(conn_id):
        raise HTTPException(status_code=409, detail="知识库未就绪")
    state.knowledge.ensure_loaded(conn_id)
    removed = state.knowledge.remove_graph_edge(conn_id, body.from_table, body.to_table, body.source)
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


@router.put("/{conn_id}/graph/layout")
async def save_graph_layout(conn_id: str, body: GraphLayoutRequest) -> dict:
    """持久化 2D 关系图布局坐标（写各表 tk.layout 并落盘；不动嵌入指纹、不触发重嵌）。"""
    _have(conn_id)
    state = get_state()
    if not state.knowledge.is_built(conn_id):
        raise HTTPException(status_code=409, detail="知识库未就绪")
    state.knowledge.ensure_loaded(conn_id)
    saved = state.knowledge.set_layout(conn_id, body.layout)
    return {"saved": saved, "layout": state.knowledge.graph_layout(conn_id)}


class GraphPinRequest(BaseModel):
    from_table: str
    to_table: str
    from_col: str | None = None
    to_col: str | None = None


class GraphConfirmRequest(BaseModel):
    from_table: str | None = None   # None = 确认/拒绝全部
    # P1-3 单边粒度：四个字段齐备时精确匹配单条 draft 边（方向可反）
    to_table: str | None = None
    from_col: str | None = None
    to_col: str | None = None


@router.post("/{conn_id}/graph/edges/pin")
async def pin_graph_edge(conn_id: str, body: GraphPinRequest) -> dict:
    """红边"保留"：给匹配正式边打 pinned 标记（不再判红；重建保留）。"""
    _have(conn_id)
    state = get_state()
    if not state.knowledge.is_built(conn_id):
        raise HTTPException(status_code=409, detail="知识库未就绪")
    state.knowledge.ensure_loaded(conn_id)
    n = state.knowledge.pin_graph_edge(
        conn_id, body.from_table, body.to_table, body.from_col, body.to_col)
    return {"pinned": n, "graph": state.knowledge.graph(conn_id)}


@router.post("/{conn_id}/graph/confirm")
async def confirm_graph_drafts(conn_id: str, body: GraphConfirmRequest | None = None) -> dict:
    """确认 draft 边 → 写入正式图谱。

    - 无参：确认全部；只给 from_table：该表双向批量（旧语义）
    - 给全 from_table/to_table/from_col/to_col：精确单边（P1-3 逐边裁决）
    """
    _have(conn_id)
    state = get_state()
    if not state.knowledge.is_built(conn_id):
        raise HTTPException(status_code=409, detail="知识库未就绪")
    state.knowledge.ensure_loaded(conn_id)
    b = body or GraphConfirmRequest()
    added = state.knowledge.confirm_graph_edges(
        conn_id, from_table=b.from_table, to_table=b.to_table,
        from_col=b.from_col, to_col=b.to_col)
    # 一并回传最新正式边：确认后的 llm 边立即可见（前端图无需整页刷新）
    return {"confirmed": added, "llm_draft_edges": state.knowledge.llm_graph_edges(conn_id),
            "edges": state.knowledge.graph(conn_id)["edges"]}


@router.post("/{conn_id}/graph/reject")
async def reject_graph_drafts(conn_id: str, body: GraphConfirmRequest | None = None) -> dict:
    """拒绝 draft 边（从 draft 列表移除；重建时重新提案）。粒度同 confirm。"""
    _have(conn_id)
    state = get_state()
    if not state.knowledge.is_built(conn_id):
        raise HTTPException(status_code=409, detail="知识库未就绪")
    state.knowledge.ensure_loaded(conn_id)
    b = body or GraphConfirmRequest()
    removed = state.knowledge.reject_graph_edges(
        conn_id, from_table=b.from_table, to_table=b.to_table,
        from_col=b.from_col, to_col=b.to_col)
    return {"rejected": removed, "llm_draft_edges": state.knowledge.llm_graph_edges(conn_id),
            "edges": state.knowledge.graph(conn_id)["edges"]}


@router.get("/{conn_id}/retrieve")
async def retrieve(conn_id: str, q: str = "", table: str | None = None, k: int = 10) -> dict:
    _have(conn_id)
    state = get_state()
    cards = await state.knowledge.retrieve(conn_id, q, table, k)
    return {"count": len(cards), "cards": [c.to_dict() for c in cards]}


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


class AnnotateTableRequest(BaseModel):
    table: str
    include_samples: bool | None = None  # None = 跟随运行时授权开关


@router.post("/{conn_id}/annotate-table")
async def annotate_table_ep(conn_id: str, body: AnnotateTableRequest) -> dict:
    """单表重新注释（审核镜头「重试」/手动刷新单表入口）：只跑该表阶段一，提案落 draft。

    与构建共用注释缓存——LLM 失败的表无缓存条目，重试自然重新调用。
    """
    _have(conn_id)
    state = get_state()
    state.knowledge.ensure_loaded(conn_id)
    kb = state.knowledge
    tk = kb.semantic_store._tables.get(conn_id, {}).get(body.table)
    if tk is None:
        raise HTTPException(status_code=404, detail=f"表不存在：{body.table}")
    # 互斥：单表注释也在构建/同步窗口外执行（防提案写入与构建互踩）
    try:
        state.build_jobs.begin_op(conn_id, "confirm")
    except JobBusyError:
        raise HTTPException(status_code=409, detail="构建/同步已在运行")
    try:
        rt = state.runtime.get()
        include_samples = rt.kb_ai_annotation_samples if body.include_samples is None else body.include_samples
        from app.core.schema import get_schema as _get_schema
        schema = await _get_schema(state, conn_id)
        sub = kb.build_service._schema_subset(schema, {body.table})
        from app.knowledge.ddl_context import ddls_from_schema, generate_ddls_all, truncate_samples
        try:
            ddl_map = await generate_ddls_all(state, conn_id)
        except Exception as e:
            logger.warning("[kb.api] conn=%s 单表注释实时 DDL 失败，回退快照合成：%s", conn_id, e)
            ddl_map = {}
        for tname, ddl in ddls_from_schema(sub).items():
            ddl_map.setdefault(tname, ddl)
        ddl = ddl_map.get(body.table, tk.ddl)
        samples: dict[str, dict[str, list]] = {}
        if include_samples and rt.kb_sample_rows > 0:
            from app.core.schema import sample_values
            try:
                samples[body.table] = await sample_values(state, conn_id, body.table, rt.kb_sample_rows)
            except Exception as e:
                logger.warning("[kb.api] conn=%s 单表抽样失败 table=%s：%s", conn_id, body.table, e)
                samples[body.table] = {}
        from app.knowledge.annotator import annotate_table
        items, source = await annotate_table(
            state, conn_id, body.table, ddl, schema,
            truncate_samples(samples) if samples else None,
            cache=kb.annotation_cache(conn_id),
        )
        added = await kb.annotate_drafts_async(conn_id, items)
        # 单表重试成功 → 从 round.failed_tables 摘除
        rnd = kb.semantic_store.get_round(conn_id)
        failed = rnd.get("failed_tables") or []
        if body.table in failed:
            kb.semantic_store.set_round(
                conn_id, failed_tables=[t for t in failed if t != body.table])
        logger.info("[kb.api] conn=%s 单表注释完成 table=%s source=%s items=%s added=%s",
                    conn_id, body.table, source, len(items), added)
        return {"table": body.table, "source": source, "items": len(items), "added": added}
    finally:
        state.build_jobs.end_op(conn_id)


@router.post("/{conn_id}/confirm")
async def confirm(conn_id: str, body: ConfirmRequest) -> dict:
    """人工确认草案为权威（确认后检索优先）。"""
    _have(conn_id)
    state = get_state()
    # 互斥（D1）：构建/同步在跑时草案仍在写入，禁止确认
    try:
        state.build_jobs.begin_op(conn_id, "confirm")
    except JobBusyError:
        raise HTTPException(status_code=409, detail="构建/同步已在运行")
    try:
        n = await state.knowledge.confirm(conn_id, body.table, body.column, table_only=body.table_only)
    finally:
        state.build_jobs.end_op(conn_id)
    return {"confirmed": n}


@router.post("/{conn_id}/reject")
async def reject(conn_id: str, body: RejectRequest) -> dict:
    """拒绝草案注释（按表/列撤下：AI 内容清空回 none）。"""
    _have(conn_id)
    state = get_state()
    n = await state.knowledge.reject(conn_id, body.table, body.column)
    return {"rejected": n}


@router.post("/{conn_id}/reject-comment")
async def reject_comment(conn_id: str, body: RejectCommentRequest) -> dict:
    """拒绝草案注释（审查页逐列 ✕）。"""
    _have(conn_id)
    state = get_state()
    n = await state.knowledge.reject_comment(conn_id, body.table, body.column)
    return {"rejected": n}


@router.patch("/{conn_id}/table")
async def edit_table(conn_id: str, body: TableEditRequest) -> dict:
    """人工编辑单表知识（详情面板两块）：表/列注释 + 向量化片段覆盖。"""
    _have(conn_id)
    state = get_state()
    try:
        result = await state.knowledge.edit_table_knowledge(
            conn_id, body.table,
            table_comment=body.table_comment,
            column_comments=[c.model_dump() for c in body.column_comments]
            if body.column_comments else None,
            vector_text=body.vector_text,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"表或列不存在：{e}") from e
    return result


@router.get("/{conn_id}/tags")
async def tags(conn_id: str) -> dict:
    """标签库 + 表→标签 绑定。"""
    _have(conn_id)
    state = get_state()
    return state.knowledge.tags(conn_id)


@router.post("/{conn_id}/annotate-tags")
async def annotate_tags(conn_id: str) -> dict:
    """AI 生成逐表描述 + 领域标签（2026-09 版本制：不落库，全集挂 round 待审核应用）。"""
    _have(conn_id)
    state = get_state()
    r = await annotate_domain(state, conn_id)
    if r.get("domain_list"):
        state.knowledge.semantic_store.set_round(conn_id, tags_new=r["domain_list"])
    return r


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
    ok = state.knowledge.create_tag(conn_id, body.name, body.description, body.color)
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
                                        description=body.description, color=body.color)
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


@router.get("/{conn_id}/concepts")
async def concepts(conn_id: str) -> dict:
    """概念字典条目列表（draft 待确认 + confirmed）。"""
    _have(conn_id)
    state = get_state()
    state.knowledge.ensure_loaded(conn_id)
    return {"concepts": [c.to_dict() for c in state.knowledge.concept_store.list(conn_id)]}


@router.post("/{conn_id}/concepts/confirm")
async def confirm_concept(conn_id: str, body: TagNameRequest) -> dict:
    """人工确认概念条目（draft → confirmed，kb_read 按列查概念对确认项生效）。"""
    _have(conn_id)
    state = get_state()
    state.knowledge.ensure_loaded(conn_id)
    ok = state.knowledge.concept_store.confirm(conn_id, body.name)
    if ok:
        state.knowledge._save_conn(conn_id)
    return {"confirmed": ok}


@router.post("/{conn_id}/concepts/reject")
async def reject_concept(conn_id: str, body: TagNameRequest) -> dict:
    """拒绝概念条目（移除）。"""
    _have(conn_id)
    state = get_state()
    state.knowledge.ensure_loaded(conn_id)
    ok = state.knowledge.concept_store.reject(conn_id, body.name)
    if ok:
        state.knowledge._save_conn(conn_id)
    return {"rejected": ok}


@router.get("/{conn_id}/filters")
async def filters(conn_id: str) -> dict:
    """表级过滤器列表（draft 待确认 + confirmed）。"""
    _have(conn_id)
    state = get_state()
    state.knowledge.ensure_loaded(conn_id)
    return {"filters": [f.to_dict() for f in state.knowledge.filter_store.dump_objects(conn_id)]}


@router.post("/{conn_id}/filters/confirm")
async def confirm_filter(conn_id: str, body: TableNameRequest) -> dict:
    """人工确认表级过滤器（draft → confirmed，查询注入对确认项生效）。"""
    _have(conn_id)
    state = get_state()
    state.knowledge.ensure_loaded(conn_id)
    ok = state.knowledge.filter_store.confirm(conn_id, body.table)
    if ok:
        state.knowledge._save_conn(conn_id)
    return {"confirmed": ok}


@router.post("/{conn_id}/filters/reject")
async def reject_filter(conn_id: str, body: TableNameRequest) -> dict:
    """拒绝表级过滤器（移除，不再注入）。"""
    _have(conn_id)
    state = get_state()
    state.knowledge.ensure_loaded(conn_id)
    ok = state.knowledge.filter_store.reject(conn_id, body.table)
    if ok:
        state.knowledge._save_conn(conn_id)
    return {"rejected": ok}
