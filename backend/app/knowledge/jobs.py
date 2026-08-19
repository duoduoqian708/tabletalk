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

BuildFn = Callable[[Callable[[str, int], None]], Awaitable[dict]]


class BuildJob:
    __slots__ = ("conn_id", "task", "progress", "started_at", "cancelled")

    def __init__(self, conn_id: str, task: asyncio.Task) -> None:
        self.conn_id = conn_id
        self.task = task
        self.progress: dict[str, Any] = {"stage": "排队中", "percent": 0, "done": False, "error": None}
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.cancelled = False


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
        def report(stage: str, percent: int) -> None:
            if job.cancelled:
                raise asyncio.CancelledError()
            job.progress.update({"stage": stage, "percent": min(100, max(0, int(percent)))})

        stats = await build_fn(report)
        job.progress.update({"stage": "完成", "percent": 100, "done": True, "error": None})
        if _is_current():
            state.connections.set_kb_status(conn_id, "pending_review")
        return stats
    except asyncio.CancelledError:
        # 用户取消（或旧任务被替换）：仅当前任务才清理状态，避免污染新任务
        if _is_current():
            state.knowledge.clear(conn_id)
            state.connections.set_kb_status(conn_id, "none")
        job.progress.update({"stage": "已取消", "percent": 0, "done": True, "error": "cancelled"})
        raise
    except Exception as e:  # noqa: BLE001
        if _is_current():
            state.connections.set_kb_status(conn_id, "none")
        job.progress.update({"stage": "失败", "percent": 0, "done": True, "error": str(e)})
        raise
