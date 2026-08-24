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
import time
from typing import Any, Awaitable, Callable

STAGES = ["发现结构", "抽样取值", "生成注释文档", "构图", "向量化", "落盘"]

# 四阶段进度条（同一弹窗内四条独立进度；enums 受数据授权门控，未授权时无进度更新属预期）
PHASES = [
    {"key": "annotate", "label": "AI 正在处理"},
    {"key": "tags", "label": "AI 标签提取"},
    {"key": "graph", "label": "AI 关系识别"},
    {"key": "enums", "label": "AI 枚举字典"},
]

BuildFn = Callable[[Callable[[str, int, str | None, str | None], None]], Awaitable[dict]]


def _new_progress() -> dict[str, Any]:
    return {
        "stage": "排队中", "percent": 0, "done": False, "error": None, "detail": None,
        "phases": [
            {"key": p["key"], "label": p["label"], "percent": 0, "detail": None}
            for p in PHASES
        ],
    }


class BuildJob:
    __slots__ = ("conn_id", "task", "progress", "started_at", "cancelled", "event")

    def __init__(self, conn_id: str, task: asyncio.Task) -> None:
        self.conn_id = conn_id
        self.task = task
        self.progress = _new_progress()
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
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

    try:
        # 进度上报 + 协作取消检查点（安全点：无 live DB 句柄）
        # phase: None=全局 stage；'annotate'/'tags'/'graph'=更新对应独立阶段进度
        def report(stage: str, percent: int, detail: str | None = None,
                   phase: str | None = None) -> None:
            if job.cancelled:
                raise asyncio.CancelledError()
            job.progress.update({"stage": stage, "percent": min(100, max(0, int(percent))), "detail": detail})
            if phase:
                for p in job.progress.get("phases", []):
                    if p["key"] == phase:
                        p["percent"] = min(100, max(0, int(percent)))
                        p["detail"] = detail
                        p["stage"] = stage
                        break
            job.event.set()  # 唤醒 SSE 订阅者

        stats = await build_fn(report)
        job.progress.update({"stage": "完成", "percent": 100, "done": True, "error": None})
        for p in job.progress.get("phases", []):
            p["percent"] = 100
        job.event.set()
        if _is_current():
            state.connections.set_kb_status(conn_id, "pending_review")
        return stats
    except asyncio.CancelledError:
        # 用户取消（或旧任务被替换）：仅当前任务才清理状态，避免污染新任务
        if _is_current():
            state.knowledge.clear(conn_id)
            state.connections.set_kb_status(conn_id, "none")
        job.progress.update({"stage": "已取消", "percent": 0, "done": True, "error": "cancelled"})
        job.event.set()
        raise
    except Exception as e:  # noqa: BLE001
        if _is_current():
            state.connections.set_kb_status(conn_id, "none")
        job.progress.update({"stage": "失败", "percent": 0, "done": True, "error": str(e)})
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
                    continue
                samples = {}
                if rt.kb_sample_rows > 0:
                    for t in schema["tables"]:
                        try:
                            samples[t["name"]] = await sample_values(state, c.id, t["name"], rt.kb_sample_rows)
                        except Exception:
                            samples[t["name"]] = {}
                result = await state.knowledge.sync(c.id, schema, samples)
                if result.get("changed"):
                    import logging
                    logging.getLogger(__name__).info(
                        "[kb.sync] %s 增量同步：+%s表 -%s表 变更%s表",
                        c.name, result.get("tables_added", 0),
                        result.get("tables_removed", 0), result.get("tables_changed", 0),
                    )
            except Exception:  # noqa: BLE001 - 单个连接失败不影响其他
                continue

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(self._sleep)
        except asyncio.CancelledError:
            pass
