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

STAGES = ["发现结构", "抽样取值", "生成注释文档", "构图", "向量化", "落盘"]

# 三阶段独立进度条 + 每阶段子步（段7）：阶段一=逐表注释；阶段二=划分→审校自检；
# 阶段三=全局扫描→候选裁决；子步由 annotator 内部 on_progress 上报。
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
        {"key": "selfcheck", "label": "审校自检"},
    ]},
    {"key": "graph", "label": "AI 关系识别", "steps": [
        {"key": "global", "label": "全局扫描"},
        {"key": "verify", "label": "候选裁决"},
    ]},
]

# 全局 overall 条权重窗口（段7.3）：phase 内部 0-100 映射到全局单调进度；
# phase=None 的全局原始值（发现结构/抽样）直接透传。
# graph 窗口 60→100：AI 段 0→74，收尾（FK 构图+落盘）瞬时由 done 一帧收满——
# 不存在"阶段条满但构建未完"的假完成段。
# 窗口须高于前置原始值上界（非授权时抽样跳过，自动取窗口起点）。
PHASE_WINDOW = {
    "annotate": (16, 45),
    "tags":     (45, 60),
    "graph":    (60, 100),
}

BuildFn = Callable[[Callable[..., None]], Awaitable[dict]]


def _step_label(phase_key: str, step_key: str) -> str | None:
    for p in PHASES:
        if p["key"] != phase_key:
            continue
        for s in p.get("steps", []):
            if s["key"] == step_key:
                return s["label"]
        break
    return step_key


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
             "busy": False,
             "steps": [{"key": s["key"], "label": s["label"]} for s in p.get("steps", [])]}
            for p in PHASES
        ],
    }


class BuildJob:
    __slots__ = ("conn_id", "task", "progress", "started_at", "cancelled", "event")

    def __init__(self, conn_id: str, task: asyncio.Task) -> None:
        self.conn_id = conn_id
        self.task = task
        self.progress = _new_progress()
        self.started_at = utcnow_iso()
        self.cancelled = False
        self.event = asyncio.Event()  # 进度更新通知（SSE 推送用）


class BuildJobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, BuildJob] = {}

    def start(self, conn_id: str, build_fn: BuildFn) -> BuildJob:
        """启动后台构建任务；旧任务若仍在跑则标记取消（由其在下个阶段边界自杀）。"""
        old = self._jobs.get(conn_id)
        if old is not None and not old.task.done():
            old.cancelled = True
        job = BuildJob(conn_id, None)  # type: ignore[arg-type]  # task 下面赋值
        self._jobs[conn_id] = job
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
        job = self._jobs.get(conn_id)
        return job is not None and not job.task.done() and not job.cancelled

    def cancel(self, conn_id: str) -> bool:
        """协作式取消：置标志，任务在下一个阶段边界自停（不打断 DB 调用）。"""
        job = self._jobs.get(conn_id)
        if job is None or job.task.done():
            return False
        job.cancelled = True
        return True

    def finish(self, conn_id: str) -> None:
        self._jobs.pop(conn_id, None)


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

    last_phase: str | None = None
    last_step: str | None = None

    try:
        # 进度上报 + 协作取消检查点（安全点：无 live DB 句柄）
        # phase: None=全局 stage（发现结构/抽样/构图/向量化/落盘，percent 即 overall）；
        #   'annotate'/'tags'/'graph'=阶段内 0-100，overall 按 PHASE_WINDOW 映射。
        # step: 子步 key（per_table/partition/selfcheck/global/verify）+ step_index/total。
        def report(stage: str, percent: int, detail: str | None = None,
                   phase: str | None = None, step: str | None = None,
                   step_index: int | None = None, step_total: int | None = None,
                   busy: bool = False, check_cancel: bool = True) -> None:
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
                        break
            job.event.set()  # 唤醒 SSE 订阅者

        stats = await build_fn(report)
        job.progress.update({"stage": "完成", "percent": 100, "done": True, "error": None})
        for p in job.progress.get("phases", []):
            p["percent"] = 100
        job.event.set()
        if _is_current():
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
            state.knowledge.clear(conn_id)
            state.connections.set_kb_status(conn_id, "none")
        job.progress.update({
            "stage": "已取消", "percent": 0, "done": True, "error": "cancelled",
            "error_at": {"phase": last_phase, "step": last_step},
        })
        job.event.set()
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("[kb.build] conn=%s 构建失败：%s", conn_id, e)
        if _is_current():
            state.connections.set_kb_status(conn_id, "none")
        # 失败定位到子步（段7.4）：percent 保留卡死点，error_at 标注失败的阶段/子步
        job.progress.update({
            "stage": "失败", "done": True, "error": str(e),
            "error_at": {"phase": last_phase, "step": last_step},
        })
        job.event.set()
        raise


class SyncLoop:
    """知识库增量同步周期任务：按 kb_sync_minutes 遍历 ready 连接，
    指纹对比（get_schema 复用 30s 缓存）→ 有变化则抽样并增量同步。
    与构建任务同连接互斥；失败静默（保持 ready，下周期再试）。
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._sleep = 30  # 检查节拍（秒）；实际间隔由 kb_sync_minutes 控制

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

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
            if state.build_jobs.is_running(c.id):
                continue
            try:
                schema = await get_schema(state, c.id)
                if not state.knowledge.needs_sync(c.id, schema):
                    logger.debug("[kb.sync] conn=%s 结构无变化，跳过", c.id)
                    continue
                samples = {}
                # 严格零采样：定时同步仅在运行时授权 ai 采样开关时抽取（与 store.sync 解析一致）
                if rt.kb_ai_annotation_samples and rt.kb_sample_rows > 0:
                    for t in schema["tables"]:
                        try:
                            samples[t["name"]] = await sample_values(state, c.id, t["name"], rt.kb_sample_rows)
                        except Exception as e:
                            logger.warning("[kb.sync] conn=%s 抽样失败 table=%s：%s", c.id, t["name"], e)
                            samples[t["name"]] = {}
                result = await state.knowledge.sync(c.id, schema, samples)
                if result.get("changed"):
                    logger.info(
                        "[kb.sync] %s(%s) 增量同步：+%s表 -%s表 变更%s表",
                        c.name, c.id, result.get("tables_added", 0),
                        result.get("tables_removed", 0), result.get("tables_changed", 0),
                    )
            except Exception as e:  # noqa: BLE001 - 单个连接失败不影响其他
                logger.warning("[kb.sync] 连接 %s(%s) 同步检查失败：%s", c.name, c.id, e)
                continue

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
