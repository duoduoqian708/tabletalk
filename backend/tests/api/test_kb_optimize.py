"""阶段 1 优化回归：注释缓存 / diff 全量重构 / rename 继承 / round 对比区落盘 / 三档参数。"""
from __future__ import annotations

import asyncio

import pytest

from app.knowledge.annotator import annotate_table, annotation_input_hash


def _table_schema(app_state, conn_id: str, table: str) -> dict:
    schema = app_state.knowledge.semantic_store._schema.get(conn_id, {})
    return {
        "tables": [t for t in schema.get("tables", []) if t["name"] == table],
        "columns": [c for c in schema.get("columns", []) if c["table"] == table],
        "foreign_keys": [f for f in schema.get("foreign_keys", []) if f["table"] == table],
    }


def _proposals(app_state, conn_id: str) -> dict[str, int]:
    """{表名: 提案数}（表级 + 列级 has_proposal）。"""
    out = {}
    for name, tk in app_state.knowledge.semantic_store._tables.get(conn_id, {}).items():
        n = int(tk.has_proposal) + sum(1 for ci in tk.columns.values() if ci.has_proposal)
        out[name] = n
    return out


async def _schema(app_state, conn_id: str) -> dict:
    from app.core.schema import get_schema
    return await get_schema(app_state, conn_id)


async def _build_via_api(client, conn_id, body: dict | None = None):
    r = await client.post(f"/api/v1/knowledge/{conn_id}/build", json=body or {})
    assert r.status_code == 200, r.text
    for _ in range(200):
        p = (await client.get(f"/api/v1/knowledge/{conn_id}/build/progress")).json()
        if p.get("done"):
            assert not p.get("error"), p["error"]
            return p
        await asyncio.sleep(0.05)
    raise AssertionError("build 超时")


async def test_build_params_validation(client, conn_id):
    r = await client.post(f"/api/v1/knowledge/{conn_id}/build",
                          json={"annotate_mode": "bogus"})
    assert r.status_code == 422
    r = await client.post(f"/api/v1/knowledge/{conn_id}/build",
                          json={"tag_mode": "bogus"})
    assert r.status_code == 422


async def test_annotation_cache_hit(app_state, conn_id):
    """annotate_table 缓存：二次调用命中（source=cache）；DDL 变化失效；空产出不缓存。"""
    # 先建表壳（annotate_table 只依赖 schema 参数，shell 非必需，但保持真实形态）
    await app_state.knowledge.build(conn_id, await _schema(app_state, conn_id), None)
    sub = _table_schema(app_state, conn_id, "orders")
    from app.knowledge.ddl_context import ddls_from_schema
    ddl = ddls_from_schema(sub)["orders"]
    cache: dict = {}
    items1, src1 = await annotate_table(app_state, conn_id, "orders", ddl, sub, None, cache=cache)
    assert src1 in ("mock", "llm") and items1
    assert cache["orders"]["input_hash"], "缓存条目应带 input_hash"
    items2, src2 = await annotate_table(app_state, conn_id, "orders", ddl, sub, None, cache=cache)
    assert src2 == "cache" and items2 == items1
    _items3, src3 = await annotate_table(app_state, conn_id, "orders", ddl + "\n-- changed",
                                         sub, None, cache=cache)
    assert src3 != "cache"
    # 空产出不缓存：直接断言 _cache_put 拒绝空 items
    from app.knowledge.annotator import _cache_put
    before = dict(cache)
    _cache_put(cache, "ghost", "h", [])
    assert cache == before, "空 items 不应写缓存"


async def test_diff_rebuild_zero_proposals_on_unchanged(app_state, conn_id):
    """diff 重构：结构未变 → 零新提案；改一张表注释 → 仅该表重提案。"""
    st = app_state
    await st.knowledge.build(conn_id, await _schema(st, conn_id), None)
    assert any(v > 0 for v in _proposals(st, conn_id).values()), "首建应有提案"
    await st.knowledge.confirm_all(conn_id)
    assert all(v == 0 for v in _proposals(st, conn_id).values()), "确认后无提案"
    # 结构未变的 diff 重构 → 全库零提案
    await st.knowledge.build(conn_id, await _schema(st, conn_id), None,
                             annotate_mode="diff", tag_mode="keep")
    assert all(v == 0 for v in _proposals(st, conn_id).values()), _proposals(st, conn_id)
    # orders 表注释变化 → 仅 orders 重提案
    schema = await _schema(st, conn_id)
    for t in schema["tables"]:
        if t["name"] == "orders":
            t["comment"] = "销售订单主单（走查测试改注释）"
    await st.knowledge.build(conn_id, schema, None, annotate_mode="diff", tag_mode="keep")
    props = _proposals(st, conn_id)
    assert props.get("orders", 0) > 0, "orders 表应有新提案"
    others = {k: v for k, v in props.items() if k != "orders"}
    assert all(v == 0 for v in others.values()), f"未变表不应有提案：{others}"


async def test_full_rebuild_reproposes_all(app_state, conn_id):
    """full 重构（结构未变）：全表重新提案（缓存命中不改变提案语义，只省 LLM）。"""
    st = app_state
    await st.knowledge.build(conn_id, await _schema(st, conn_id), None)
    await st.knowledge.confirm_all(conn_id)
    await st.knowledge.build(conn_id, await _schema(st, conn_id), None,
                             annotate_mode="full", tag_mode="keep")
    props = _proposals(st, conn_id)
    assert props.get("orders", 0) > 0 and props.get("customers", 0) > 0, props


async def test_tag_mode_fresh_starts_clean(app_state, conn_id):
    """tag_mode=fresh：旧标签清空，新划分产 draft 待审（版本制：划分结果不自动落库）。"""
    st = app_state
    await st.knowledge.build(conn_id, await _schema(st, conn_id), None)
    await st.knowledge.confirm_all(conn_id)
    # 手造 confirmed 标签（用户审核应用/手建的等价形态；annotate_domain 是版本制不落库）
    st.knowledge.upsert_tags(conn_id, [{"name": "订单", "description": "人工确认的旧域"}])
    st.knowledge.confirm_tag(conn_id, "订单")
    lib_before = st.knowledge.semantic_store._tags.get(conn_id, {})
    assert lib_before.get("订单", {}).get("status") == "confirmed"
    await st.knowledge.build(conn_id, await _schema(st, conn_id), None,
                             annotate_mode="full", tag_mode="fresh")
    lib_after = st.knowledge.semantic_store._tags.get(conn_id, {})
    assert not lib_after, "fresh 应清空旧标签库（新划分走 tags_new 待审核应用）"
    round_meta = st.knowledge.semantic_store.get_round(conn_id)
    assert round_meta.get("tags_new"), "fresh 重建应产出新标签全集供审核"


async def test_domain_anchor_prompt_renders():
    """锚点 prompt：existing_tags 注入模板；allow_rename 时输出契约含 renamed_from。"""
    from app.knowledge.annotator import _domain_partition_prompt
    p = _domain_partition_prompt("PORTRAITS", 3, 5, 10,
                                 existing_tags="- 订单（订单数据域）", allow_rename=True)
    assert "订单（订单数据域）" in p and "renamed_from" in p
    p2 = _domain_partition_prompt("PORTRAITS", 3, 5, 10)
    assert "renamed_from" not in p2


async def test_rename_inherits_knowledge(app_state, conn_id):
    """rename 继承：知识/向量/TK.name 改挂新表名，不重新注释。"""
    st = app_state
    await st.knowledge.build(conn_id, await _schema(st, conn_id), None)
    await st.knowledge.confirm_all(conn_id)  # 提案生效后才有表级向量（向量延迟到确认后）
    tabs = st.knowledge.semantic_store._tables[conn_id]
    assert "orders" in tabs
    old_tk = tabs["orders"]
    old_comment = old_tk.comment
    old_vec = st.knowledge.retrieval_service._table_vec.get(conn_id, {}).get("orders")
    assert old_vec, "确认后 orders 应有表级向量"
    # 模拟改名：orders → sales_orders（列签名不变）
    schema = await _schema(st, conn_id)
    for t in schema["tables"]:
        if t["name"] == "orders":
            t["name"] = "sales_orders"
    for c in schema["columns"]:
        if c["table"] == "orders":
            c["table"] = "sales_orders"
    for f in schema["foreign_keys"]:
        if f["table"] == "orders":
            f["table"] = "sales_orders"
        if f["ref_table"] == "orders":
            f["ref_table"] = "sales_orders"
    result = await st.knowledge.sync(conn_id, schema, None)
    assert result.get("changed"), result
    tabs2 = st.knowledge.semantic_store._tables[conn_id]
    assert "sales_orders" in tabs2 and "orders" not in tabs2
    new_tk = tabs2["sales_orders"]
    assert new_tk.name == "sales_orders", "TK.name 必须同步改写"
    assert new_tk.comment == old_comment, "改名应继承知识"
    assert st.knowledge.retrieval_service._table_vec[conn_id].get("sales_orders") == old_vec


async def test_round_persists_across_restart(app_state, conn_id):
    """审核对比区（baseline/diff）随快照落盘：内存清空后从磁盘恢复。"""
    st = app_state
    await st.knowledge.build(conn_id, await _schema(st, conn_id), None)
    round_before = st.knowledge.semantic_store.get_round(conn_id)
    assert round_before.get("diff"), "构建后应有 round diff"
    # 模拟重启：清内存（_auto/_user 是 ensure_loaded 的守卫键，必须一并清）→ 从磁盘恢复
    st.knowledge._auto.pop(conn_id, None)
    st.knowledge.semantic_store._user.pop(conn_id, None)
    st.knowledge.semantic_store._tables.pop(conn_id, None)
    st.knowledge.semantic_store._round.pop(conn_id, None)
    st.knowledge.ensure_loaded(conn_id)
    round_after = st.knowledge.semantic_store.get_round(conn_id)
    assert round_after.get("diff") == round_before.get("diff")
    # baseline：首轮构建时旧库为空 → baseline 可能不存在；有 baseline 时必须往返一致
    if round_before.get("baseline") is not None:
        assert round_after.get("baseline") == round_before.get("baseline")
    # 二轮重构（已有旧知识）→ baseline 必然存在且落盘往返一致
    await st.knowledge.confirm_all(conn_id)
    await st.knowledge.build(conn_id, await _schema(st, conn_id), None,
                             annotate_mode="full", tag_mode="keep")
    rb2 = st.knowledge.semantic_store.get_round(conn_id)
    assert rb2.get("baseline"), "二轮重构必须有 baseline"
    st.knowledge._auto.pop(conn_id, None)
    st.knowledge.semantic_store._user.pop(conn_id, None)
    st.knowledge.semantic_store._tables.pop(conn_id, None)
    st.knowledge.semantic_store._round.pop(conn_id, None)
    st.knowledge.ensure_loaded(conn_id)
    assert st.knowledge.semantic_store.get_round(conn_id).get("baseline") == rb2.get("baseline")


async def test_annotation_cache_persists(app_state, conn_id):
    """注释缓存随快照落盘：重启后缓存仍在（二次构建零 LLM）。"""
    st = app_state
    await st.knowledge.build(conn_id, await _schema(st, conn_id), None)
    cache_before = dict(st.knowledge.build_service._annotation_cache.get(conn_id, {}))
    assert cache_before, "mock 构建后应有缓存条目"
    st.knowledge._auto.pop(conn_id, None)
    st.knowledge.semantic_store._user.pop(conn_id, None)
    st.knowledge.semantic_store._tables.pop(conn_id, None)
    st.knowledge.build_service._annotation_cache.pop(conn_id, None)
    st.knowledge.ensure_loaded(conn_id)
    cache_after = st.knowledge.build_service._annotation_cache.get(conn_id, {})
    assert cache_after == cache_before


async def test_sync_job_transitions_to_pending_review(client, conn_id, app_state):
    """sync 任务化：结构变化 → sync job → 有提案 → kb_status 转 pending_review（修复现网缺口）。"""
    await _build_via_api(client, conn_id)
    r = await client.post(f"/api/v1/knowledge/{conn_id}/confirm-all")
    assert r.json()["kb_status"] == "ready"
    # 真实 DDL 变更：demo 库新建一张表
    await app_state.pools.execute(conn_id, "CREATE TABLE zz_optcheck (id INTEGER PRIMARY KEY, note TEXT)")
    # 手动同步：快速路径判定有变化 → 启动 job
    r = await client.post(f"/api/v1/knowledge/{conn_id}/sync")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("changed") is True, body
    # 轮询进度直到 done（与构建同一通道）
    for _ in range(200):
        p = (await client.get(f"/api/v1/knowledge/{conn_id}/build/progress")).json()
        if p.get("done"):
            assert not p.get("error"), p["error"]
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("sync job 超时")
    st = (await client.get(f"/api/v1/knowledge/{conn_id}/status")).json()
    assert st["kb_status"] == "pending_review", "sync 产出提案后应进待审（修复缺口）"
    ov = (await client.get(f"/api/v1/knowledge/{conn_id}/overview")).json()
    assert any(t["name"] == "zz_optcheck" for t in ov.get("tables", [])), "新表应出现在知识库"


async def test_sync_no_change_fast_path(client, conn_id, app_state):
    """无变化同步：同步快速返回，不起 sync job（job 槽仍是上次 build）。"""
    await _build_via_api(client, conn_id)
    r = await client.post(f"/api/v1/knowledge/{conn_id}/confirm-all")
    r = await client.post(f"/api/v1/knowledge/{conn_id}/sync")
    assert r.status_code == 200, r.text
    assert r.json().get("changed") is False
    job = app_state.build_jobs.get(conn_id)
    assert job is None or job.kind == "build", "无变化不应启动 sync job"


async def test_failed_tables_cleared_on_next_round(app_state, conn_id):
    """failed_tables 跨轮清理：上轮残留的失败表在本轮构建成功后必须清空。"""
    st = app_state
    await st.knowledge.build(conn_id, await _schema(st, conn_id), None)
    # 手动预置上轮残留（模拟上轮有失败表）
    st.knowledge.semantic_store.set_round(conn_id, failed_tables=["ghost_table"])
    await st.knowledge.build(conn_id, await _schema(st, conn_id), None,
                             annotate_mode="full", tag_mode="keep")
    assert st.knowledge.semantic_store.get_round(conn_id).get("failed_tables") == [], \
        "本轮构建成功后 failed_tables 应被清空"


async def test_progress_frame_carries_kind(client, conn_id):
    """进度帧携带 kind：build job 帧 kind=build（sync=sync 由 sync 测试覆盖）。"""
    r = await client.post(f"/api/v1/knowledge/{conn_id}/build", json={})
    assert r.status_code == 200
    for _ in range(200):
        p = (await client.get(f"/api/v1/knowledge/{conn_id}/build/progress")).json()
        if p.get("done"):
            break
        await asyncio.sleep(0.05)
    assert p.get("kind") == "build", p