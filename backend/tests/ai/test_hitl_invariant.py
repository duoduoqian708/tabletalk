"""HITL 架构不变式回归测试：写操作永不无确认自动执行。

这是产品「执行权在人」的物理保证（PRD §7.9/§7.10）。无论未来如何改写循环/
工具/框架，这些测试一旦被破坏必须立即修复——写操作无确认执行=信任塌方。
"""

from app.ai.tools import execute_tool


async def test_run_dml_never_autoexecutes(app_state, conn_id):
    """AI run_dml 工具：只返回 review 卡 + 预估行数，绝不实际执行。"""
    out = await execute_tool(
        app_state, "run_dml",
        {"sql": "UPDATE products SET price = price * 1.1 WHERE category_id = 1"},
        conn_id,
    )
    assert out.card["verdict"] == "review"      # 永不 allow/executed
    assert out.result.get("needs_confirm") is True
    assert "rows" not in out.result             # 没返回已改数据（没执行）
    # 关键：工具侧没有 confirm 参数，物理上无法自动执行——这一步本就锁定


async def test_run_dml_no_where_blocked(app_state, conn_id):
    """无 WHERE 的写操作被 BLOCK，绝不 review/applied。"""
    out = await execute_tool(app_state, "run_dml", {"sql": "DELETE FROM products"}, conn_id)
    assert out.card["verdict"] == "block"
    assert out.card["tier"] == "dml"


async def test_post_query_write_requires_confirm(app_state, conn_id):
    """POST /query 的写操作：不 confirm 不执行（只有 review+preview），confirm 才 executed。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import app
    from app.config import get_token

    transport = ASGITransport(app=app)
    headers = {"X-Cleared-Token": get_token()}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        payload = {
            "connection_id": conn_id,
            "sql": "UPDATE products SET price = price * 1.1 WHERE category_id = 1",
            "origin": "ai",
        }
        # 不 confirm → review（预估行数，未执行）
        r1 = await c.post("/api/v1/query", json=payload)
        b1 = r1.json()
        assert r1.status_code == 200 and b1["verdict"] == "review"
        assert b1.get("needs_confirm") is True
        assert "affected_rows" not in b1      # 未执行的关键标志

        # confirm → executed + affected_rows
        r2 = await c.post("/api/v1/query", json={**payload, "confirm": True})
        b2 = r2.json()
        assert b2["verdict"] == "executed"
        assert b2.get("affected_rows", 0) >= 1
