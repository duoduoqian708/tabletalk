"""审计日志路由。"""
from __future__ import annotations

import datetime as _dt
import sqlite3
import time

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.state import get_state

router = APIRouter(prefix="/api/v1", tags=["audit"])


@router.get("/audit")
async def audit_log(
    connection: str | None = None,
    origin: str | None = None,
    tier: str | None = None,
    verdict: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    from_ts: str | None = None,
    to_ts: str | None = None,
    report_id: str | None = None,
    source: str | None = None,
    q: str | None = None,
    exception: bool = False,
    unread_only: bool = False,
    cursor: int | None = None,
) -> dict:
    state = get_state()
    cursor_mode = cursor is not None or bool(q) or exception or unread_only
    if cursor_mode:
        res = state.audit.page(
            connection=connection, origin=origin, tier=tier, verdict=verdict,
            q=q, exception=exception, unread_only=unread_only,
            from_ts=from_ts, to_ts=to_ts,
            before_id=int(cursor) if cursor and cursor > 0 else None,
            limit=min(limit, 200),
        )
        next_cursor = res["items"][-1]["_id"] if res["has_more"] and res["items"] else None
        return {"items": res["items"], "next_cursor": next_cursor}
    full = state.audit.list(
        connection=connection, origin=origin, tier=tier, verdict=verdict,
        from_ts=from_ts, to_ts=to_ts, report_id=report_id, source=source,
    )
    total = len(full)
    window = full[::-1][offset:offset + limit]  # 最新在前
    return {"count": total, "entries": window}


@router.get("/audit/egress")
async def audit_egress(
    connection: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> dict:
    """出网清单报表：按模型/模式聚合，给合规与审计看。"""
    state = get_state()
    full = state.audit.list(verdict="egress", connection=connection, from_ts=from_ts, to_ts=to_ts)
    by_model: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    for e in full:
        m = (e.get("manifest") or {}).get("model") or e.get("manifest", {}).get("provider") or "unknown"
        by_model[m] = by_model.get(m, 0) + 1
        mode = (e.get("manifest") or {}).get("mode") or "unknown"
        by_mode[mode] = by_mode.get(mode, 0) + 1
    return {"total": len(full), "by_model": by_model, "by_mode": by_mode, "entries": full[::-1][:100]}


@router.get("/audit/weekly")
async def audit_weekly(
    connection: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> dict:
    """周报摘要：按周聚合写操作、Top 表、异常提示。"""
    state = get_state()
    full = state.audit.list(connection=connection, from_ts=from_ts, to_ts=to_ts)
    # 按周分组（ts 前 10 为 YYYY-MM-DD，取周）
    from collections import Counter, defaultdict
    import datetime
    weekly: dict[str, int] = defaultdict(int)
    top_tables: Counter = Counter()
    for e in full:
        ts = e.get("ts", "")[:10]
        try:
            dt = datetime.datetime.strptime(ts, "%Y-%m-%d")
            week = dt.strftime("%Y-W%V")
            weekly[week] += 1
        except Exception:
            weekly["unknown"] += 1
        for t in e.get("tables") or []:
            top_tables[t] += 1
    # 异常模式：深夜批量 UPDATE（22:00-05:00 且 verdict=review/block 且 tier=dml）
    anomalies: list[dict] = []
    for e in full:
        ts = e.get("ts", "")
        try:
            hour = int(ts[11:13]) if len(ts) >= 13 else 12
            if hour >= 22 or hour <= 5:
                if e.get("tier") == "dml" and e.get("verdict") in ("review", "block"):
                    anomalies.append({"ts": ts, "sql": e.get("sql", "")[:80], "verdict": e.get("verdict")})
                    if len(anomalies) >= 5:
                        break
        except Exception:
            pass
    return {
        "weekly": dict(weekly),
        "top_tables": top_tables.most_common(5),
        "anomalies": anomalies,
        "total": len(full),
    }


@router.get("/audit/summary")
async def audit_summary(
    connection: str | None = None,
    verdict: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> dict:
    """信号区聚合：按判定/来源/层级计数 + 拦截率 + AI 尝试 DDL 数。"""
    state = get_state()
    full = state.audit.list(
        connection=connection, verdict=verdict, from_ts=from_ts, to_ts=to_ts,
    )
    total = len(full)
    by_verdict: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    for e in full:
        v = e.get("verdict", "?")
        o = e.get("origin", "?")
        t = e.get("tier", "?")
        by_verdict[v] = by_verdict.get(v, 0) + 1
        by_origin[o] = by_origin.get(o, 0) + 1
        by_tier[t] = by_tier.get(t, 0) + 1
    blocked = by_verdict.get("block", 0)
    ai_ddl = sum(1 for e in full if e.get("origin") == "ai" and e.get("tier") == "ddl")
    review = by_verdict.get("review", 0)
    return {
        "total": total,
        "by_verdict": by_verdict,
        "by_origin": by_origin,
        "by_tier": by_tier,
        "blocked_rate": (blocked / total) if total else 0,
        "ai_ddl_count": ai_ddl,
        "review_count": review,
    }


@router.get("/audit/signal")
async def audit_signal(connection: str | None = None) -> dict:
    """状态栏徽章轮询：未读异常 / 待审审批 / 今日拦截与待确认。极轻量。"""
    state = get_state()
    unread = state.audit.unread_exception_count(connection)
    pending = len(state.approvals.list(status="pending"))
    today_start = time.strftime("%Y-%m-%dT00:00:00")
    rows = state.audit.list(connection=connection, from_ts=today_start)
    blocked = sum(1 for e in rows if e.get("verdict") == "block")
    review = sum(1 for e in rows if e.get("verdict") == "review")
    return {
        "unread_exceptions": unread,
        "pending_approvals": pending,
        "today": {"blocked": blocked, "review": review},
    }


@router.get("/audit/stats")
async def audit_stats(scope: str = "30d", connection: str | None = None) -> dict:
    """三档统计：today=小时桶(当日0点起)；7d/30d=天桶。时区=本地。"""
    now = _dt.datetime.now()
    if scope == "today":
        since = now.strftime("%Y-%m-%dT00:00:00"); fmt = "%Y-%m-%dT%H:00:00"; gran = "hour"
    elif scope == "7d":
        since = (now - _dt.timedelta(days=6)).strftime("%Y-%m-%dT00:00:00"); fmt = "%Y-%m-%dT00:00:00"; gran = "day"
    else:
        since = (now - _dt.timedelta(days=29)).strftime("%Y-%m-%dT00:00:00"); fmt = "%Y-%m-%dT00:00:00"; gran = "day"
    buckets = get_state().audit.stats_buckets(connection, since, fmt)
    return {"scope": scope, "granularity": gran, "buckets": buckets}


class AckRequest(BaseModel):
    state: str = "ack"


@router.post("/audit/{audit_id}/ack")
async def ack_audit(audit_id: int, req: AckRequest) -> dict:
    try:
        get_state().audit.ack(int(audit_id))
    except sqlite3.IntegrityError:
        pass
    return {"ok": True}
