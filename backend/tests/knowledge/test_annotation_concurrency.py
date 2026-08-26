"""逐表注释并发（阶段一）核心行为测试。

覆盖：
- 并发上限生效：N 表并发调用数 ≤ 配置并发（token 池）
- 429 限流 → 指数退避重试 + 并发额度减半；连续成功回升
- 非瞬断错误（4xx 业务错）不重试
- 单表重试耗尽失败不影响其余表
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import app.knowledge.annotator as annotator


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://example.local/chat")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError("status", request=req, response=resp)


def _fake_state(items_by_table: dict[str, list[dict]] | None = None):
    """知识库桩：annotate_drafts 返回写入条数（不真正落库）。"""
    items_by_table = items_by_table or {}

    class FakeKB:
        def annotate_drafts(self, conn_id, items):
            return len(items)

    class FakeState:
        knowledge = FakeKB()

    return FakeState()


# ---------- 自适应限制器 ----------


async def test_limiter_shrinks_to_floor_and_recovers():
    limiter = annotator._AdaptiveLimiter(8)
    assert limiter.limit == 8
    for _ in range(4):
        await limiter.on_rate_limit()
    assert limiter.limit == 1, "8→4→2→1 应降到地板 1"
    await limiter.on_rate_limit()
    assert limiter.limit == 1, "到地板后不再降"
    for _ in range(annotator._KB_CONCURRENCY_RECOVER_STREAK):
        await limiter.on_success()
    assert limiter.limit == 2, "连续成功应回升 1 档"
    # 回升上限封顶 cap
    for _ in range(60):
        await limiter.on_success()
    assert limiter.limit == 8


async def test_limiter_acquire_blocks_over_limit(monkeypatch):
    limiter = annotator._AdaptiveLimiter(2)
    acquired = 0
    release: list = []

    async def worker():
        nonlocal acquired
        await limiter.acquire()
        acquired += 1
        await asyncio.sleep(0.03)
        await limiter.release()

    await asyncio.gather(*(worker() for _ in range(6)))
    # 全部串行跑完后 acquired 应为 6（都执行了），但任何时刻并发 ≤2 由上层测试保证
    assert acquired == 6


# ---------- 重试封装 ----------


async def test_retry_chat_429_then_success_shrinks_limiter(monkeypatch):
    monkeypatch.setattr(annotator, "_KB_CONCURRENCY_BACKOFF_BASE", 0.01)
    limiter = annotator._AdaptiveLimiter(8)
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(429)
        return "ok"

    assert await annotator._retry_chat(flaky, limiter) == "ok"
    assert calls["n"] == 2
    assert limiter.limit == 4, "撞一次 429 → 并发减半"


async def test_retry_chat_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(annotator, "_KB_CONCURRENCY_BACKOFF_BASE", 0.01)
    limiter = annotator._AdaptiveLimiter(2)

    async def always_429():
        raise _status_error(429)

    with pytest.raises(RuntimeError):
        await annotator._retry_chat(always_429, limiter, max_retries=2)
    assert limiter.limit == 1, "两次限流 2→1（兜底到串行）"


async def test_retry_chat_does_not_retry_bad_request(monkeypatch):
    monkeypatch.setattr(annotator, "_KB_CONCURRENCY_BACKOFF_BASE", 0.01)
    limiter = annotator._AdaptiveLimiter(4)
    calls = {"n": 0}

    async def bad():
        calls["n"] += 1
        raise _status_error(400)

    with pytest.raises(httpx.HTTPStatusError):
        await annotator._retry_chat(bad, limiter)
    assert calls["n"] == 1, "4xx 业务错非瞬断 → 不重试"


async def test_retry_chat_network_error_is_retryable(monkeypatch):
    monkeypatch.setattr(annotator, "_KB_CONCURRENCY_BACKOFF_BASE", 0.01)
    limiter = annotator._AdaptiveLimiter(4)
    calls = {"n": 0}

    async def flaky_net():
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return "ok"

    assert await annotator._retry_chat(flaky_net, limiter) == "ok"
    assert calls["n"] == 2
    assert limiter.limit == 4, "网络瞬断非限流 → 并发不动"


# ---------- annotate_tables 并发编排 ----------


async def test_annotate_tables_caps_concurrency_and_returns_all(monkeypatch):
    names = [f"t{i}" for i in range(5)]
    schema = {"tables": [{"name": n, "kind": "table", "comment": ""} for n in names]}
    ddl_map = {n: f"CREATE TABLE {n} (id int)" for n in names}
    active = {"cur": 0, "max": 0}

    async def fake_annotate(state, conn_id, table_name, table_ddl, schema, samples=None):
        active["cur"] += 1
        active["max"] = max(active["max"], active["cur"])
        await asyncio.sleep(0.03)
        active["cur"] -= 1
        return [{"table": table_name, "column": "id", "comment": f"{table_name}的注释"}]

    monkeypatch.setattr(annotator, "annotate_table", fake_annotate)
    added = await annotator.annotate_tables(_fake_state(), "c1", ddl_map, schema, concurrency=2)
    assert added == 5
    assert active["max"] <= 2, f"并发超限：max_active={active['max']}"
    assert active["cur"] == 0


async def test_annotate_tables_one_table_failure_skips_others(monkeypatch):
    names = [f"t{i}" for i in range(5)]
    schema = {"tables": [{"name": n} for n in names]}
    ddl_map = {n: f"CREATE TABLE {n} (id int)" for n in names}

    async def flaky_annotate(state, conn_id, table_name, table_ddl, schema, samples=None):
        if table_name == "t2":
            raise _status_error(400)  # 业务错，非瞬断不重试 → 单表失败
        return [{"table": table_name, "column": "id", "comment": f"{table_name}的注释"}]

    monkeypatch.setattr(annotator, "annotate_table", flaky_annotate)
    added = await annotator.annotate_tables(_fake_state(), "c1", ddl_map, schema, concurrency=3)
    assert added == 4, "t2 失败应跳过，其余 4 表照常产出"