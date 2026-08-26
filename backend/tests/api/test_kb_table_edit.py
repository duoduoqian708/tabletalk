"""按表编辑知识 API（PATCH /table）端到端契约。

构建（默认 mock 网关）→ 编辑表注释 + 向量化片段覆盖 → overview 回显。
只写知识字段，type 恒 table_schema（前端只读展示，接口不接收）。
"""
from __future__ import annotations

import asyncio


async def _drain_build(app_state, conn_id: str) -> None:
    """等后台构建任务结束，避免测试退出时残留 pending task。"""
    job = app_state.build_jobs.get(conn_id)
    if job is not None:
        try:
            await asyncio.wait_for(job.task, timeout=30)
        except Exception:
            job.cancelled = True


async def _build_and_drain(client, app_state, conn_id: str) -> None:
    r = await client.post(f"/api/v1/knowledge/{conn_id}/build", json={})
    assert r.status_code == 200, r.text
    await _drain_build(app_state, conn_id)


async def test_patch_table_edits_comment_and_vector_override(client, app_state, conn_id):
    await _build_and_drain(client, app_state, conn_id)
    ov = await client.get(f"/api/v1/knowledge/{conn_id}/overview")
    assert ov.status_code == 200
    tables = ov.json()["tables"]
    assert tables, "构建后应有表"
    tname = tables[0]["name"]

    r = await client.patch(
        f"/api/v1/knowledge/{conn_id}/table",
        json={"table": tname, "table_comment": "由 API 测试写入的表注释",
              "vector_text": "人工覆盖的向量化片段——API 测试"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vector_text"] == "人工覆盖的向量化片段——API 测试"
    assert body["vector_override"] == "人工覆盖的向量化片段——API 测试"

    ov2 = await client.get(f"/api/v1/knowledge/{conn_id}/overview")
    t2 = next(t for t in ov2.json()["tables"] if t["name"] == tname)
    assert t2["comment"] == "由 API 测试写入的表注释"
    assert t2["comment_status"] == "confirmed", "人工写入表注释 → 权威 confirmed"
    assert t2["vector_override"] == "人工覆盖的向量化片段——API 测试"
    assert t2["vector_text"] == "人工覆盖的向量化片段——API 测试"


async def test_patch_table_unknown_returns_404(client, app_state, conn_id):
    await _build_and_drain(client, app_state, conn_id)
    r = await client.patch(
        f"/api/v1/knowledge/{conn_id}/table",
        json={"table": "no_such_table", "table_comment": "x"},
    )
    assert r.status_code == 404