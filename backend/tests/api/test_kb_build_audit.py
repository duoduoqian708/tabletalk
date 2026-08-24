"""构建确认审计留痕测试：用户点"开始构建"即写审计（spec §3.8），含授权与触发来源。"""
from __future__ import annotations

import asyncio


async def _drain_build(app_state, conn_id: str) -> None:
    """等后台构建任务结束，避免测试退出时残留 pending task。"""
    job = app_state.build_jobs.get(conn_id)
    if job is not None:
        try:
            await asyncio.wait_for(job.task, timeout=30)
        except (asyncio.TimeoutError, Exception):
            job.cancelled = True


async def test_build_confirmation_audited(client, app_state, conn_id):
    """构建发起即写审计：origin=kb_build, status=confirmed, extra 含 trigger/include_samples。"""
    r = await client.post(
        f"/api/v1/knowledge/{conn_id}/build",
        json={"include_samples": False, "trigger": "init"},
    )
    assert r.status_code == 200
    # 审计写入发生在任务启动前（同步），无需等待构建完成即可断言
    entries = app_state.audit.list(connection=conn_id, origin="kb_build")
    assert len(entries) == 1
    e = entries[0]
    assert e["status"] == "confirmed"
    assert e["verdict"] == "allow"
    assert e["tier"] == "read"
    assert e["source"] == "manual"
    # log() 的额外字段落 extra_json，list() 展开到顶层
    assert e["trigger"] == "init"
    assert e["include_samples"] is False
    # sql 列是单行摘要注释
    assert "trigger=init" in e["sql"]
    assert "include_samples=False" in e["sql"]
    await _drain_build(app_state, conn_id)


async def test_build_explicit_samples_authorized_audited(client, app_state, conn_id):
    """正路用例：显式 include_samples=true 的构建请求，审计 extra 记录授权为 true。"""
    r = await client.post(
        f"/api/v1/knowledge/{conn_id}/build",
        json={"include_samples": True, "trigger": "init"},
    )
    assert r.status_code == 200
    entries = app_state.audit.list(connection=conn_id, origin="kb_build")
    assert len(entries) == 1
    e = entries[0]
    assert e["trigger"] == "init"
    assert e["include_samples"] is True
    assert "include_samples=True" in e["sql"]
    await _drain_build(app_state, conn_id)


async def test_build_rejects_bad_trigger(client, app_state, conn_id):
    """trigger 非法 → 422，且不留审计记录（校验先于留痕）。"""
    r = await client.post(
        f"/api/v1/knowledge/{conn_id}/build", json={"trigger": "hax"}
    )
    assert r.status_code == 422
    assert app_state.audit.list(connection=conn_id, origin="kb_build") == []


async def test_build_default_trigger_is_init(client, app_state, conn_id):
    """不带 body 的旧调用兼容：默认 trigger=init、include_samples=False（spec §3.8 默认不授权）且照常留痕。"""
    r = await client.post(f"/api/v1/knowledge/{conn_id}/build")
    assert r.status_code == 200
    entries = app_state.audit.list(connection=conn_id, origin="kb_build")
    assert len(entries) == 1
    assert entries[0]["trigger"] == "init"
    assert entries[0]["include_samples"] is False
    await _drain_build(app_state, conn_id)
