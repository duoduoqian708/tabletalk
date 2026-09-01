"""脚本任务调度：beat 循环（镜像 kb sync_loop），周期扫注册表，到期 enabled job 拉起。

在飞互斥：同一任务同一时刻只跑一个实例（超出的到期触发直接跳过）。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import TYPE_CHECKING

from app.tasks import cron

if TYPE_CHECKING:
    from app.state import AppState

logger = logging.getLogger(__name__)

BEAT_SECONDS = 30


class JobScheduler:
    def __init__(self, sleep: int = BEAT_SECONDS) -> None:
        self._task: asyncio.Task | None = None
        self._sleep = sleep
        self._inflight: set[str] = set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    def is_running(self, name: str) -> bool:
        return name in self._inflight

    async def tick(self) -> list[str]:
        from app.state import get_state  # noqa: PLC0415 - 延迟导入避免循环

        state = get_state()
        state.jobs.reload()
        now = dt.datetime.now()
        started: list[str] = []
        for job in state.jobs.list():
            if not job.enabled:
                continue
            if not cron.is_due(job.cron, now):
                continue
            if job.name in self._inflight:
                continue  # 上一实例仍在飞 → 跳过本次触发
            self._inflight.add(job.name)
            started.append(job.name)
            asyncio.create_task(self._run_guarded(state, job.name))
        return started

    async def _run_guarded(self, state: "AppState", job_name: str) -> None:
        from app.tasks import runner

        try:
            await runner.run_job(state, job_name, trigger="schedule")
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning("[jobs] %s 执行异常：%s", job_name, e)
        finally:
            self._inflight.discard(job_name)

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.warning("[jobs] tick 异常：%s", e)
                await asyncio.sleep(self._sleep)
        except asyncio.CancelledError:
            pass