"""stats 时间桶聚合（今日=小时桶 / 近N天=天桶）。"""
from __future__ import annotations

from app.audit.logger import AuditLogger
from app.core.timeutil import utc_from_local_midnight


def _seed(lg: AuditLogger):
    lg.log(connection="c", origin="ai", tier="read", verdict="allow", status="s", sql="SELECT 1")
    lg.log(connection="c", origin="ai", tier="dml", verdict="block", status="s", sql="DELETE FROM t")
    lg.log(connection="c", origin="ai", tier="dml", verdict="review", status="s", sql="UPDATE t SET a=1 WHERE id=1")


def test_hour_buckets_today(tmp_path):
    lg = AuditLogger(tmp_path); _seed(lg)
    # 存储为 UTC，今日边界必须用本地 00:00 对应的 UTC 时刻（与 API 口径一致）
    today_start = utc_from_local_midnight()
    buckets = lg.stats_buckets(connection=None, since_ts=today_start, fmt="%Y-%m-%dT%H:00:00")
    total = sum(b["total"] for b in buckets)
    assert total == 3
    assert sum(b["block"] for b in buckets) == 1
    assert sum(b["review"] for b in buckets) == 1
    assert all(len(b["bucket"]) == 13 for b in buckets)  # YYYY-MM-DDTHH


def test_day_buckets_range(tmp_path):
    lg = AuditLogger(tmp_path); _seed(lg)
    buckets = lg.stats_buckets(connection=None, since_ts="2000-01-01T00:00:00", fmt="%Y-%m-%dT00:00:00")
    assert len(buckets) >= 1
    assert all(bucket.endswith("T00:00:00") for bucket in [b["bucket"] for b in buckets])


def test_connection_scoped(tmp_path):
    lg = AuditLogger(tmp_path); _seed(lg)
    lg.log(connection="other", origin="ai", tier="dml", verdict="block", status="s", sql="DELETE FROM x")
    buckets = lg.stats_buckets(connection="other", since_ts="2000-01-01T00:00:00", fmt="%Y-%m-%dT00:00:00")
    assert sum(b["total"] for b in buckets) == 1


async def test_endpoint_scopes(client):
    r = await client.get("/api/v1/audit/stats", params={"scope": "7d"})
    assert r.status_code == 200
    body = r.json()
    assert "buckets" in body and body["granularity"] == "day"
    r2 = await client.get("/api/v1/audit/stats", params={"scope": "today"})
    assert r2.json()["granularity"] == "hour"
