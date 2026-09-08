"""知识库构建任务管理器：后台 asyncio 任务 + 阶段进度 + 协作式取消。

构建是接入流程的强制步骤：kb_status 状态机（none→building→pending_review→ready）。
进度经 GET /build/progress 轮询（500ms）。

取消采用**协作式**（不 task.cancel()）：取消只是置标志，任务在阶段边界
（report 回调点，无活跃 DB 句柄）自抛 CancelledError 收尾——避免在任意 await
点打断导致 aiosqlite 连接泄漏 worker 线程（进程挂起）。

旧任务被新任务替换时（用户重新构建），旧任务在其 report 点自杀；所有状态写入
前都校验"自己仍是当前 job"，避免旧任务污染新任务。
"""
from __future__ import annotations

import asyncio
import logging
import time
from app.core.timeutil import utcnow_iso
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# 三阶段独立进度条 + 每阶段子步：阶段一=逐表注释；阶段二=领域划分；
# 阶段三=全局扫描；子步由 annotator 内部 on_progress 上报。
#
# 「最后一段补完 = 完成」不变式：graph 条窗口 60→100，AI 段（global/verify）内部
# 0→74（映射 60→89.6），其后 FK 构图 + 落盘瞬时完成（无独立进度段），
# run_build_job 收尾时统一置满各条 + done。graph 条打满 = 构建完成 → 关浮卡可审核。
# 向量化不占构建进度：它延迟到人工确认后（confirm → _reembed_tables），确认前
# AI 草案不入向量文本，构建期嵌入纯属白做。
PHASES = [
    {"key": "annotate", "label": "AI 正在处理", "steps": [
        {"key": "per_table", "label": "逐表注释"},
    ]},
    {"key": "tags", "label": "AI 标签提取", "steps": [
        {"key": "partition", "label": "领域划分"},
    ]},
    {"key": "graph", "label": "AI 关系识别", "steps": [
        {"key": "global", "label": "全局扫描"},
    ]},
]

# 全局 overall 条权重窗口（段7.3）：phase 内部 0-100 映射到全局单调进度；
# phase=None 的全局原始值（发现结构/抽样）直接透传。
# 各窗口起点 = 前段终点（发现结构原始值上界 10）→ 阶段起跑零跳变：
#   阶段一 0 表 = 10%，逐表 +0.89%；tags/graph 并行起跑 = 50%。
# graph 窗口 50→100：AI 段 0→74（映射 50→89.6），收尾（FK 构图+落盘）瞬时由 done 一帧收满——
# 不存在"阶段条满但构建未完"的假完成段。tags/graph 并行帧由 run_build_job 的 max(ov, prev) 兜底单调。
PHASE_WINDOW = {
    "annotate": (10, 50),
    "tags":     (50, 65),
    "graph":    (50, 100),
}

BuildFn = Callable[[Callable[..., None]], Awaitable[dict]]


class JobBusyError(Exception):
    """连接已有构建/同步/确认/放弃操作在跑（互斥拒绝）。"""


# 连接级互斥操作种类（非 build 的轻量前台操作：注册即占坑，finally 释放）
OP_KINDS = ("sync", "confirm", "discard")


def _step_label(phase_key: str, step_key: str) -> str | None:
    for p in PHASES:
        if p["key"] != phase_key:
            continue
        for s in p.get("steps", []):
            if s["key"] == step_key:
                return s["label"]
        break
    return step_key


def _touched_for_sync(state: Any, conn_id: str, schema: dict[str, Any]) -> set[str]:
    """增量同步的 touched 表集合（只对 touched 抽样/注释）；粗算失败退回全表。

    rename 前后表名都纳入（采样宁多勿漏）。
    """
    try:
        kb = state.knowledge
        old = kb.semantic_store._schema.get(conn_id) or {}
        if not old:
            return {t["name"] for t in schema.get("tables", [])}
        touched, diff = kb.build_service.compute_touched(old, schema)
        renames = kb.build_service.match_renames(
            old, schema, set(diff["removed_tables"]), set(diff["added_tables"]))
        touched |= {r["from"] for r in renames} | {r["to"] for r in renames}
        return touched
    except Exception:  # noqa: BLE001 - 粗算失败保守退回全表
        return {t["name"] for t in schema.get("tables", [])}


def apply_sync_result_status(state: Any, conn_id: str, result: dict[str, Any]) -> None:
    """sync 成功后的状态流转（手动 sync job 与 SyncLoop 共用，修复现网缺口）：

    有新提案（结构变化需要过目）→ pending_review（审核入口出现）；
    无变化/纯 rename 继承 → 保持 ready（不打扰）。
    前置条件：连接当前为 ready（pending_review 期间 sync 被互斥/前置检查拦住）。
    """
    if not (result or {}).get("changed"):
        return
    has_changes = bool(
        result.get("ai_docs_added", 0)
        or result.get("tables_added", 0)
        or result.get("tables_changed", 0)
    )
    if not has_changes:
        return
    cfg = state.connections.get(conn_id)
    if cfg.kb_status == "ready":
        state.connections.set_kb_status(conn_id, "pending_review")
        logger.info("[kb.sync] conn=%s 增量产出提案 → pending_review（待审核）", conn_id)


def _overall(phase: str | None, percent: int) -> int:
    """阶段内部百分比 → 全局 overall 单调进度（0-100）。"""
    pct = max(0, min(100, int(percent)))
    if phase is None:
        return pct
    lo, hi = PHASE_WINDOW.get(phase, (0, 100))
    return lo + (hi - lo) * pct // 100


def _new_progress() -> dict[str, Any]:
    return {
        "stage": "排队中", "percent": 0, "done": False, "error": None, "detail": None,
        "phases": [
            {"key": p["key"], "label": p["label"], "percent": 0, "detail": None,
             "step": None, "step_label": None, "step_index": None, "step_total": None,
             "busy": False, "live": None,
             "steps": [{"key": s["key"], "label": s["label"]} for s in p.get("steps", [])]}
            for p in PHASES
        ],
    }


class BuildJob:
    __slots__ = ("conn_id", "task", "progress", "started_at", "cancelled", "event", "kind")

    def __init__(self, conn_id: str, task: asyncio.Task, kind: str = "build") -> None:
        self.conn_id = conn_id
        self.task = task
        self.kind = kind  # build | sync（收尾状态机分流）
        self.progress = _new_progress()
        self.started_at = utcnow_iso()
        self.cancelled = False
        self.event = asyncio.Event()  # 进度更新通知（SSE 推送用）


class BuildJobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, BuildJob] = {}
        self._ops: dict[str, str] = {}  # conn_id → sync|confirm|discard（前台互斥占坑）

    # ---- 连接级互斥（D1）：begin/end 必须成对且中间无首 await 前置检查 ----
    def begin_op(self, conn_id: str, kind: str) -> None:
        """注册前台互斥操作（同步、无 await：检查+占坑原子）。

        与 build 任务、其他前台操作互斥；冲突抛 JobBusyError（API 层转 409）。
        """
        if kind not in OP_KINDS:
            raise ValueError(f"unknown op kind: {kind}")
        if self.is_running(conn_id) or conn_id in self._ops:
            raise JobBusyError(conn_id)
        self._ops[conn_id] = kind

    def end_op(self, conn_id: str) -> None:
        self._ops.pop(conn_id, None)

    def start(self, conn_id: str, build_fn: BuildFn, kind: str = "build") -> BuildJob:
        """启动后台构建/同步任务；旧任务若仍在跑则标记取消（由其在下个阶段边界自杀）。

        kind: build=全量构建 | sync=增量同步（成功后状态机分流不同，见 run_build_job）。
        前台互斥操作（sync/confirm/discard）在跑时拒绝启动（不与前台操作并发）。
        """
        if conn_id in self._ops:
            raise JobBusyError(conn_id)
        old = self._jobs.get(conn_id)
        if old is not None and not old.task.done():
            old.cancelled = True
        job = BuildJob(conn_id, None, kind=kind)  # type: ignore[arg-type]  # task 下面赋值
        self._jobs[conn_id] = job
        job.progress["kind"] = kind  # 进度帧携带任务类型（前端区分 build/sync 展示）
        job.task = asyncio.create_task(run_build_job(job, build_fn))
        return job

    def get(self, conn_id: str) -> BuildJob | None:
        return self._jobs.get(conn_id)

    def progress(self, conn_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(conn_id)
        if job is None:
            return None
        return dict(job.progress)

    def is_running(self, conn_id: str) -> bool:
        if conn_id in self._ops:
            return True
        job = self._jobs.get(conn_id)
        return job is not None and not job.task.done() and not job.cancelled

    def cancel(self, conn_id: str) -> bool:
        """协作式取消：置标志，任务在下一个阶段边界自停（不打断 DB 调用）。"""
        job = self._jobs.get(conn_id)
        if job is None or job.task.done():
            return False
        job.cancelled = True
        return True

async def run_build_job(job: BuildJob, build_fn: BuildFn) -> dict:
    """后台构建任务包装：进度上报 + 协作取消 + 状态机流转。

    build_fn 接收 report(stage, percent) 回调；report 是唯一取消检查点——
    所有阶段边界（结构/抽样/构图/嵌入循环/落盘）都会经过它，且彼时无活跃 DB 句柄。
    状态流转：building →（成功）pending_review →（取消/失败）none。
    所有状态写入前校验"自己仍是当前 job"（防旧任务污染新任务）。
    """
    from app.state import get_state  # noqa: PLC0415 - 延迟导入避免 state↔jobs 循环
    state = get_state()
    mgr = state.build_jobs
    conn_id = job.conn_id

    def _is_current() -> bool:
        return mgr._jobs.get(conn_id) is job

    def _reset_kb_state() -> None:
        """取消/失败后：清半成品内存，按是否已有已确认历史恢复状态（与 discard 对称）。

        旧知识磁盘工件仍在，is_built 仍 True——clear 只清内存，ensure_loaded 会从
        磁盘恢复旧库。因此有 confirmed 历史 → ready（旧知识继续可用），否则 none。
        """
        had_confirmed = state.knowledge.has_confirmed_content(conn_id)
        state.knowledge.clear(conn_id)
        state.connections.set_kb_status(conn_id, "ready" if had_confirmed else "none")

    last_phase: str | None = None
    last_step: str | None = None

    try:
        # 进度上报 + 协作取消检查点（安全点：无 live DB 句柄）
        # phase: None=全局 stage（发现结构/抽样/构图/向量化/落盘，percent 即 overall）；
        #   'annotate'/'tags'/'graph'=阶段内 0-100，overall 按 PHASE_WINDOW 映射。
        # step: 子步 key（per_table/partition/global）+ step_index/total。
        def report(stage: str, percent: int, detail: str | None = None,
                   phase: str | None = None, step: str | None = None,
                   step_index: int | None = None, step_total: int | None = None,
                   busy: bool = False, check_cancel: bool = True,
                   live: str | None = None) -> None:
            nonlocal last_phase, last_step
            if job.cancelled and check_cancel:
                raise asyncio.CancelledError()
            last_phase = phase
            last_step = step
            # 阶段2/3 可并行：顶层 percent 取历史最大值（各阶段内部只增），
            # 避免帧在 tags/graph 窗口间横跳破坏"全局单调不降"契约
            ov = _overall(phase, percent)
            prev = job.progress.get("percent", 0)
            job.progress.update({
                "stage": stage, "percent": max(ov, prev), "detail": detail,
            })
            if phase:
                for p in job.progress.get("phases", []):
                    if p["key"] == phase:
                        p["percent"] = max(0, min(100, int(percent)))
                        p["detail"] = detail
                        p["stage"] = stage
                        p["step"] = step
                        p["step_index"] = step_index
                        p["step_total"] = step_total
                        p["step_label"] = _step_label(phase, step) if step else None
                        # 心跳 busy 帧：LLM 调用期间进度不变但跑光动画；真实帧复位
                        p["busy"] = bool(busy)
                        # 流式生成尾巴（单行 LLM 输出/思考链尾文，2026-09）：浮卡进度条下展示，
                        # 证明"在输出而非卡死"；非流式调用恒 None
                        if live is not None:
                            p["live"] = live
                        break
            job.event.set()  # 唤醒 SSE 订阅者

        stats = await build_fn(report)
        job.progress.update({"stage": "完成", "percent": 100, "done": True, "error": None})
        for p in job.progress.get("phases", []):
            p["percent"] = 100
        # D3 降级可见：tags+graph 双失败 = 知识库缺骨架，判定构建失败（不进待审）
        degraded = list((stats or {}).get("degraded_phases") or [])
        if degraded:
            job.progress["degraded_phases"] = degraded
        if job.kind == "build" and "tags" in degraded and "graph" in degraded:
            logger.warning("[kb.build] conn=%s 标签与关系识别均失败，中止启用", conn_id)
            job.progress.update({
                "stage": "失败", "done": True,
                "error": "标签与关系识别均失败（已中止启用）",
            })
            if _is_current():
                _reset_kb_state()
            job.event.set()
            return stats
        job.event.set()
        if _is_current():
            # 状态机分流（2026-09 优化）：
            # - build 成功 → pending_review（进入审核镜头）
            # - sync 成功：有新提案 → pending_review（修复现网缺口：sync 提案此前永远停在
            #   ready、审核入口隐身）；无变化/无提案 → 保持 ready（不打扰）
            # - sync 失败/取消 → 保持 ready（旧知识可用，下周期再试），不判失败
            if job.kind == "sync":
                apply_sync_result_status(state, conn_id, stats or {})
            else:
                state.connections.set_kb_status(conn_id, "pending_review")
        logger.info(
            "[kb.build] conn=%s 任务完成：docs=%s ai_items=%s tags=%s edges=%s",
            conn_id,
            stats.get("docs", 0), stats.get("ai_docs_added", 0), stats.get("ai_tags_added", 0),
            stats.get("graph_edges", 0),
        )
        return stats
    except asyncio.CancelledError:
        logger.info("[kb.build] conn=%s 构建取消", conn_id)
        # 用户取消（或旧任务被替换）：仅当前任务才清理状态，避免污染新任务
        if _is_current():
            _reset_kb_state()
        job.progress.update({
            "stage": "已取消", "percent": 0, "done": True, "error": "cancelled",
            "error_at": {"phase": last_phase, "step": last_step},
        })
        job.event.set()
        raise
    except Exception as e:  # noqa: BLE001
        # exc_info：带堆栈——区分 LLM 读超时 / httpx 连接层 / 程序缺陷（断线排查核心证据）
        logger.warning("[kb.build] conn=%s 构建失败：%s", conn_id, e, exc_info=True)
        if _is_current():
            # 与取消路径对称：清理半成品内存态，避免失败残留
            _reset_kb_state()
        # 失败定位到子步（段7.4）：percent 保留卡死点，error_at 标注失败的阶段/子步
        job.progress.update({
            "stage": "失败", "done": True, "error": str(e),
            "error_at": {"phase": last_phase, "step": last_step},
        })
        job.event.set()
        raise


class SyncLoop:
    """知识库增量同步周期任务：按 kb_sync_minutes 的最小间隔遍历 ready 连接，
    指纹对比（get_schema 复用 30s 缓存）→ 有变化则抽样并增量同步。
    _sleep=30 只是 tick 节拍；每个连接记录上次处理时间，未到 minutes 间隔直接跳过
    （结构探测/日志挖掘/防漂移采样都受该间隔约束，不轰炸用户库）。
    与构建任务/前台操作经 begin_op 互斥；失败静默（保持 ready，下周期再试）。
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._sleep = 30  # tick 节拍（秒）；连接级实际间隔由 kb_sync_minutes 控制
        self._last_attempt: dict[str, float] = {}  # conn_id → 上次处理时刻（monotonic）

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _sample_distinct(self, state: Any, conn_id: str, table: str, column: str,
                               limit: int = 100) -> list[Any]:
        """轻量单列 DISTINCT 采样（防漂移用，不跑全表 sample）。"""
        from app.core.query import serialize_value

        async def _work(adapter, conn):
            quote = adapter.quote_ident
            raw = await adapter.execute(
                conn, f"SELECT DISTINCT {quote(column)} FROM {quote(table)} LIMIT {int(limit)}"
            )
            return [serialize_value(r[0]) for r in raw.rows or [] if r]

        return await state.pools.run(conn_id, _work)

    async def _check_concept_drift(self, state: Any, conn_id: str,
                                   schema: dict[str, Any]) -> dict[str, list[str]]:
        """T7 防漂移接线：confirmed 概念成员列 DISTINCT 采样 → 返回 {概念名: [新值]}。

        铁律：新值仅提示待人工确认（不自动写 canonical_enum）；采样/比对失败静默跳过。
        """
        cols_ok = {(c.get("table", ""), c.get("name", "")) for c in schema.get("columns", [])}
        out: dict[str, list[str]] = {}
        for c in state.knowledge.concept_store.list(conn_id):
            if c.status != "confirmed":
                continue
            for m in c.members:
                t, col = m.get("table", ""), m.get("column", "")
                if (t, col) not in cols_ok:
                    continue
                try:
                    sampled = await self._sample_distinct(state, conn_id, t, col)
                except Exception as e:  # pragma: no cover - 采样失败静默
                    logger.debug("[kb.sync] conn=%s 防漂移采样失败 %s.%s：%s", conn_id, t, col, e)
                    continue
                new_vals = state.knowledge.concept_store.detect_drift(conn_id, t, col, sampled)
                if new_vals:
                    out.setdefault(c.name, [])
                    out[c.name] = sorted(set(out[c.name]) | set(new_vals))
        return out

    async def tick(self) -> None:
        from app.state import get_state  # noqa: PLC0415 - 延迟导入避免循环

        state = get_state()
        minutes = state.runtime.get().kb_sync_minutes
        if minutes <= 0:
            return
        from app.core.schema import get_schema, sample_values  # noqa: PLC0415
        rt = state.runtime.get()
        for c in state.connections.list():
            if c.kb_status != "ready":
                continue
            # 连接级最小间隔：未到 kb_sync_minutes 不碰用户库（探测/挖掘/采样全在内）
            now = time.monotonic()
            if now - self._last_attempt.get(c.id, 0.0) < minutes * 60:
                continue
            self._last_attempt[c.id] = now
            # 互斥：job 通道占坑（与 build/手动 sync/confirm/discard 统一互斥）；占用中跳过本轮
            try:
                state.build_jobs.begin_op(c.id, "sync")
            except JobBusyError:
                continue
            try:
                schema = await get_schema(state, c.id)
                # T10：审计日志挖掘 → query_log 边（脱离 needs_sync 短路：
                # 结构无变化也应定时挖掘；幂等——按列对去重，重复 tick 不重复加）
                try:
                    rows = state.audit.list(connection=c.name) or []
                    rows = [r for r in rows
                            if r.get("verdict") == "allow"
                            and (r.get("sql") or "").strip()
                            and not (r.get("sql") or "").strip().startswith("--")]
                    state.knowledge.apply_query_log_edges(c.id, rows)
                except Exception as e:
                    logger.warning("[kb.sync] conn=%s 日志挖掘失败：%s", c.id, e)
                # T7：概念防漂移（定时采样比对；新值提示待确认，不自动写）
                try:
                    drift = await self._check_concept_drift(state, c.id, schema)
                    for name, new_vals in drift.items():
                        logger.info("[kb.sync] conn=%s 概念 %s 漂移新值 %s（待确认）",
                                    c.id, name, new_vals)
                except Exception as e:
                    logger.warning("[kb.sync] conn=%s 防漂移检查失败：%s", c.id, e)
                if not state.knowledge.needs_sync(c.id, schema):
                    logger.debug("[kb.sync] conn=%s 结构无变化，跳过", c.id)
                    continue
                # 严格零采样：定时同步仅在运行时授权 ai 采样开关时抽取，
                # 且只抽 touched 表（先 diff 再采样，2026-09 优化：不再全库抽样）
                samples = {}
                if rt.kb_ai_annotation_samples and rt.kb_sample_rows > 0:
                    touched = _touched_for_sync(state, c.id, schema)
                    for t in schema["tables"]:
                        if t["name"] not in touched:
                            continue
                        try:
                            samples[t["name"]] = await sample_values(state, c.id, t["name"], rt.kb_sample_rows)
                        except Exception as e:
                            logger.warning("[kb.sync] conn=%s 抽样失败 table=%s：%s", c.id, t["name"], e)
                            samples[t["name"]] = {}
                result = await state.knowledge.sync(c.id, schema, samples)
                apply_sync_result_status(state, c.id, result)
                if result.get("changed"):
                    logger.info(
                        "[kb.sync] %s(%s) 增量同步：+%s表 -%s表 变更%s表",
                        c.name, c.id, result.get("tables_added", 0),
                        result.get("tables_removed", 0), result.get("tables_changed", 0),
                    )
            except Exception as e:  # noqa: BLE001 - 单个连接失败不影响其他
                logger.warning("[kb.sync] 连接 %s(%s) 同步检查失败：%s", c.name, c.id, e)
                continue
            finally:
                state.build_jobs.end_op(c.id)

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.warning("[kb.sync] tick 异常：%s", e)
                await asyncio.sleep(self._sleep)
        except asyncio.CancelledError:
            pass
