"""S3：可选追加项——工具 options 透传 + 轻量改写接口。"""

from __future__ import annotations

from app.ai.tools.registry import execute_tool


async def test_run_query_passes_options_to_card(app_state, conn_id):
    """S3-1：LLM 顺带产出的 options 透传到 card（不参与执行）。"""
    out = await execute_tool(app_state, "run_query", {
        "sql": "SELECT * FROM orders LIMIT 5",
        "options": [
            {"id": "add_tenant", "label": "补充租户ID过滤",
             "hint": "该表一般需带 tenant_id = :current_tenant"},
            {"id": "add_paging", "label": "添加分页参数", "hint": "LIMIT 100"},
        ],
    }, conn_id)
    assert out.result["ok"] is True
    assert out.card is not None and len(out.card.get("options") or []) == 2
    assert out.card["options"][0]["label"] == "补充租户ID过滤"


async def test_run_query_options_capped_and_filtered(app_state, conn_id):
    """S3-1：options 非法项丢弃、最多 3 条。"""
    out = await execute_tool(app_state, "run_query", {
        "sql": "SELECT * FROM orders LIMIT 5",
        "options": [
            {"label": "a"}, {"label": "b"}, {"label": "c"}, {"label": "d"},
            "not-a-dict",
        ],
    }, conn_id)
    assert len(out.card.get("options") or []) == 3
    assert out.card["options"][0]["label"] == "a"


async def test_sql_option_mock_returns_original(client, app_state, conn_id):
    """S3-2：mock 下改写接口返回原 SQL + 提示（演示降级）。"""
    r = await client.post("/api/v1/ai/sql-option", json={
        "connection_id": conn_id,
        "sql": "SELECT * FROM orders",
        "option": {"label": "补充租户ID过滤", "hint": "该表一般需带 tenant_id = :current_tenant"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["sql"] == "SELECT * FROM orders"
    assert "mock" in body["note"]


async def test_sql_option_validates_input(client, app_state, conn_id):
    r = await client.post("/api/v1/ai/sql-option", json={
        "connection_id": conn_id, "sql": "", "option": {"label": "x"},
    })
    assert r.status_code == 422