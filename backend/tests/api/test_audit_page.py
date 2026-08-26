"""keyset 分页 + LIKE 搜索 + 异常/未读过滤（page()）。"""
from __future__ import annotations

from app.audit.logger import AuditLogger


def _seed(lg: AuditLogger):
    for i in range(30):
        v = ["allow", "review", "block"][i % 3]
        lg.log(connection="c", origin="ai", tier="dml", verdict=v, status="s",
               sql=f"UPDATE t{i} SET a=1 WHERE id={i}")


def test_page_desc_and_cursor(tmp_path):
    lg = AuditLogger(tmp_path); _seed(lg)
    p1 = lg.page(limit=10)
    assert len(p1["items"]) == 10 and p1["has_more"] is True
    ids1 = [e["_id"] for e in p1["items"]]
    assert ids1 == sorted(ids1, reverse=True)
    p2 = lg.page(limit=10, before_id=ids1[-1])
    ids2 = [e["_id"] for e in p2["items"]]
    assert max(ids2) < min(ids1)
    assert p2["has_more"] is True


def test_page_q_search(tmp_path):
    lg = AuditLogger(tmp_path); _seed(lg)
    p = lg.page(q="t7 ", limit=100)
    assert len(p["items"]) == 1 and "t7" in p["items"][0]["sql"]
    p_pct = lg.page(q="%", limit=100)   # 通配符须被转义
    assert p_pct["items"] == []


def test_page_exception_unread(tmp_path):
    lg = AuditLogger(tmp_path); _seed(lg)
    p = lg.page(exception=True, limit=100)
    assert len(p["items"]) == 20                       # review+block
    assert all(e["verdict"] in ("block", "review") for e in p["items"])
    lg.ack(p["items"][0]["_id"])
    p2 = lg.page(exception=True, unread_only=True, limit=100)
    assert len(p2["items"]) == 19


async def test_endpoint_cursor_mode(client, app_state):
    app_state.audit.log(connection="c", origin="ai", tier="dml", verdict="block", status="s", sql="DELETE FROM t")
    r = await client.get("/api/v1/audit", params={"exception": "true", "cursor": "0", "limit": 10})
    body = r.json()
    assert "items" in body and "next_cursor" in body
    assert body["items"][0]["verdict"] == "block"


async def test_egress_weekly_time_range(client, app_state):
    app_state.audit.log(connection="c", origin="ai", tier="read", verdict="egress",
                        status="出网", sql="-", manifest={"model": "m1"})
    future = "2099-01-01T00:00:00"
    r1 = await client.get("/api/v1/audit/egress", params={"to_ts": "2000-01-01T00:00:00"})
    assert r1.json()["total"] == 0
    r2 = await client.get("/api/v1/audit/egress", params={"to_ts": future})
    assert r2.json()["total"] == 1
    r3 = await client.get("/api/v1/audit/weekly", params={"to_ts": "2000-01-01T00:00:00"})
    assert r3.json()["total"] == 0
