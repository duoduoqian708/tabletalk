"""严格零采样门控（段3）：构建/同步 API 不授权 → sample_values 零调用。

契约：未授权 → 不抽、不落盘、不发；`sample_cols=0`。授权 → 才抽样。
API 层是隐私红线第一道闸（存储层 include_samples 只控制发送，抽样的拉取在 API 层）。
"""
from __future__ import annotations

import asyncio

import app.api.knowledge as kb_api


async def _drain_build(app_state, conn_id: str) -> None:
    """等后台构建任务结束，避免测试退出时残留 pending task。"""
    job = app_state.build_jobs.get(conn_id)
    if job is not None:
        try:
            await asyncio.wait_for(job.task, timeout=30)
        except (asyncio.TimeoutError, Exception):
            job.cancelled = True


async def _probe_samples(monkeypatch):
    """sample_values 换计数探针：调用即记录（返回空样本）。"""
    calls: list = []

    async def _counting(state, conn_id, table, per_column=10):
        calls.append((table, per_column))
        return {}

    monkeypatch.setattr(kb_api, "sample_values", _counting)
    return calls


async def test_build_without_auth_never_samples(client, app_state, conn_id, monkeypatch):
    """include_samples=False → sample_values 零调用，且 overview.sample_cols=0。"""
    calls = await _probe_samples(monkeypatch)
    r = await client.post(
        f"/api/v1/knowledge/{conn_id}/build",
        json={"include_samples": False, "trigger": "init"},
    )
    assert r.status_code == 200
    await _drain_build(app_state, conn_id)
    assert calls == [], "严格零采样：未授权构建不得调用 sample_values"
    ov = await client.get(f"/api/v1/knowledge/{conn_id}/overview")
    assert ov.status_code == 200
    assert ov.json().get("sample_cols", 0) == 0, "未授权 → 无任何采样落盘"


async def test_build_with_auth_samples_each_table(client, app_state, conn_id, monkeypatch):
    """include_samples=True → 逐表抽样。"""
    sampled = {"orders": {"status": ["P", "S"]}, "products": {"price": [9.9]}}
    calls: list = []

    async def _samples(state, conn_id, table, per_column=10):
        calls.append((table, per_column))
        return sampled.get(table, {})

    monkeypatch.setattr(kb_api, "sample_values", _samples)
    r = await client.post(
        f"/api/v1/knowledge/{conn_id}/build",
        json={"include_samples": True, "trigger": "init"},
    )
    assert r.status_code == 200
    await _drain_build(app_state, conn_id)
    tables = [t for t, _ in calls]
    assert "orders" in tables and "products" in tables, f"授权构建应逐表抽样，got {tables}"


async def test_sync_without_runtime_auth_never_samples(client, app_state, conn_id, monkeypatch):
    """手动同步：运行时 ai 采样开关关闭 → 不抽样（与 store.sync 解析同源）。"""
    rt = app_state.runtime.get()
    assert rt.kb_ai_annotation_samples is False, "默认运行时开关应为关"

    def _needs_sync(*a, **k):
        return True  # needs_sync 是同步方法；返回 True 强制走"结构变化需同步"分支

    monkeypatch.setattr(app_state.knowledge, "needs_sync", _needs_sync)
    calls = await _probe_samples(monkeypatch)
    r = await client.post(f"/api/v1/knowledge/{conn_id}/sync")
    assert r.status_code == 200, r.text
    assert calls == [], "同步在未授权时也不得抽样"


async def test_sync_with_runtime_auth_samples(client, app_state, conn_id, monkeypatch):
    """手动同步：运行时 ai 采样开关打开 → 同步才抽样。"""
    rt = app_state.runtime.get()
    assert rt.kb_ai_annotation_samples is False
    app_state.runtime.update({"kb_ai_annotation_samples": True})

    def _needs_sync(*a, **k):
        return True

    monkeypatch.setattr(app_state.knowledge, "needs_sync", _needs_sync)
    calls = await _probe_samples(monkeypatch)
    r = await client.post(f"/api/v1/knowledge/{conn_id}/sync")
    assert r.status_code == 200, r.text
    assert calls, "授权打开后同步应抽样"