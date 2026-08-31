"""R12/T7 集成：concepts API（列表/确认/拒绝）。"""
from __future__ import annotations

from app.knowledge.semantic.concepts import Concept


async def test_concepts_api(client, app_state, conn_id):
    kb = app_state.knowledge
    kb.concept_store.upsert(conn_id, Concept(
        name="订单状态",
        canonical_enum=[{"code": "P", "label": "待付款"}],
        members=[{"table": "orders", "column": "status", "mapping": "code"}],
        status="draft",
    ))
    r = await client.get(f"/api/v1/knowledge/{conn_id}/concepts")
    body = r.json()
    assert len(body["concepts"]) == 1 and body["concepts"][0]["name"] == "订单状态"

    r2 = await client.post(f"/api/v1/knowledge/{conn_id}/concepts/confirm",
                           json={"name": "订单状态"})
    assert r2.json()["confirmed"] is True
    assert kb.concept_store.get(conn_id, "订单状态").status == "confirmed"

    r3 = await client.post(f"/api/v1/knowledge/{conn_id}/concepts/reject",
                           json={"name": "订单状态"})
    assert r3.json()["rejected"] is True
    assert kb.concept_store.get(conn_id, "订单状态") is None

    # 不存在的概念：确认/拒绝返回 False
    r4 = await client.post(f"/api/v1/knowledge/{conn_id}/concepts/confirm",
                           json={"name": "不存在"})
    assert r4.json()["confirmed"] is False
