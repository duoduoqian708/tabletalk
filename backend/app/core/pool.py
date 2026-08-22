"""连接池：每连接一个小池（SQLite=1 串行，PG/MySQL 可配默认 3），并发读取不再全串行。

每个 handle 同一时刻只被一个任务持有（池内互斥，handle 自身无需锁）；
执行失败自动重连一次后归还。

架构守卫（05 §3.4 proxy 期权）：本模块不依赖 FastAPI 生命周期（`_pool_for` 仅依赖 ConnectionRegistry），
未来加透明代理（pgbouncer 式协议层）可在 `app/api/query.py` 之上新增 TCP 代理而不改核心；此为不立项的期权，仅保证新代码不把 pool 生命周期绑死在 FastAPI app 上。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from app.config import get_env
from app.core.connections import ConnectionRegistry
from app.core.dialects.base import RawResult
from app.core.dialects.registry import get_dialect


class _Handle:
    __slots__ = ("adapter", "conn")

    def __init__(self, adapter: Any, conn: Any) -> None:
        self.adapter = adapter
        self.conn = conn


class _ConnPool:
    """单连接的小池：空闲句柄队列 + 按需创建，上限 max_size。"""

    def __init__(self, registry: ConnectionRegistry, conn_id: str, max_size: int) -> None:
        self._registry = registry
        self.conn_id = conn_id
        self.max_size = max(1, max_size)
        self._handles: list[_Handle] = []
        self._free: asyncio.Queue[_Handle] = asyncio.Queue()
        self._open_lock = asyncio.Lock()

    async def _create(self) -> _Handle:
        cfg = self._registry.get(self.conn_id)
        adapter = get_dialect(cfg.dialect)
        conn = await adapter.connect(cfg.to_dialect_config())
        h = _Handle(adapter, conn)
        self._handles.append(h)
        return h

    async def acquire(self) -> _Handle:
        try:
            return self._free.get_nowait()
        except asyncio.QueueEmpty:
            pass
        if len(self._handles) < self.max_size:
            async with self._open_lock:
                if len(self._handles) < self.max_size:
                    return await self._create()
        return await self._free.get()

    def release(self, h: _Handle) -> None:
        self._free.put_nowait(h)

    async def close_all(self) -> None:
        for h in self._handles:
            await h.adapter.close(h.conn)
        self._handles.clear()
        while True:
            try:
                self._free.get_nowait()
            except asyncio.QueueEmpty:
                break


class PoolManager:
    def __init__(self, registry: ConnectionRegistry) -> None:
        self._registry = registry
        self._pools: dict[str, _ConnPool] = {}
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()

    def _pool_for(self, conn_id: str) -> _ConnPool:
        p = self._pools.get(conn_id)
        if p is None:
            cfg = self._registry.get(conn_id)
            # SQLite 单连接串行（文件锁 + 并发写语义）；PG/MySQL 用运行时可配小池
            size = 1 if cfg.dialect == "sqlite" else self._runtime_pool_size()
            p = _ConnPool(self._registry, conn_id, size)
            self._pools[conn_id] = p
        return p

    def _runtime_pool_size(self) -> int:
        try:
            from app.state import get_state

            return get_state().runtime.get().pool_size
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).warning("读取运行时 pool_size 失败，回退 env 默认值")
            return get_env().pool_size

    async def run(self, conn_id: str, fn) -> Any:
        """借出句柄执行 fn(adapter, conn)，失败自动重连一次，finally 归还。"""
        pool = self._pool_for(conn_id)
        self._inflight += 1
        self._idle.clear()
        try:
            h = await pool.acquire()
            try:
                try:
                    return await fn(h.adapter, h.conn)
                except Exception as e:
                    # 对“表/列不存在”等确定性错误不重试（重试必败且可能挂起）
                    msg = str(e).lower()
                    if any(k in msg for k in ("no such table", "no such column", "unknown column", "doesn't exist")):
                        raise
                    cfg = self._registry.get(conn_id)
                    adapter = get_dialect(cfg.dialect)
                    conn = await adapter.connect(cfg.to_dialect_config())
                    h.adapter, h.conn = adapter, conn
                    return await fn(adapter, conn)
            finally:
                pool.release(h)
        finally:
            self._inflight -= 1
            if self._inflight == 0:
                self._idle.set()

    async def execute(self, conn_id: str, sql: str) -> RawResult:
        return await self.run(conn_id, lambda a, c: a.execute(c, sql))

    async def test(self, conn_id: str) -> dict[str, Any]:
        cfg = self._registry.get(conn_id)
        adapter = get_dialect(cfg.dialect)
        t0 = time.monotonic()
        conn = None
        try:
            conn = await adapter.connect(cfg.to_dialect_config())
            ok = await adapter.is_healthy(conn)
            return {"ok": ok, "latency_ms": round((time.monotonic() - t0) * 1000, 1), "error": None}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "latency_ms": round((time.monotonic() - t0) * 1000, 1), "error": str(e)}
        finally:
            if conn is not None:
                await adapter.close(conn)

    async def test_draft(self, cfg_data: dict[str, Any]) -> dict[str, Any]:
        """不落盘测试（接入流程前置）：用待保存的配置直接连一次。

        SQLite 无"库"概念 → 放宽为"文件可读 + 能列出表"；其余方言按真实链路连库。
        """
        from app.core.connections import ConnectionConfig

        cfg = ConnectionConfig(id="draft", **cfg_data)
        adapter = get_dialect(cfg.dialect)
        t0 = time.monotonic()
        conn = None
        try:
            conn = await adapter.connect(cfg.to_dialect_config())
            ok = await adapter.is_healthy(conn)
            detail = None
            if ok and cfg.dialect == "sqlite":
                tables = await adapter.list_tables(conn)
                ok = len(tables) > 0
                if not ok:
                    detail = "文件可读但未发现任何表"
            return {"ok": ok, "latency_ms": round((time.monotonic() - t0) * 1000, 1), "error": detail}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "latency_ms": round((time.monotonic() - t0) * 1000, 1), "error": str(e)}
        finally:
            if conn is not None:
                await adapter.close(conn)

    async def close(self, conn_id: str) -> None:
        p = self._pools.pop(conn_id, None)
        if p is not None:
            await p.close_all()

    async def close_all(self) -> None:
        for conn_id in list(self._pools):
            await self.close(conn_id)

    async def rebuild(self) -> None:
        """设置变更后重建所有池：先等在场查询排空，再关闭（避免中途关连接）。"""
        await self._idle.wait()
        await self.close_all()
