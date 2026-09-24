"""统一时间戳工具：存储一律 UTC（ISO-8601 带偏移），展示由前端转本地时区。"""
from __future__ import annotations

import datetime as _dt



def utcnow_iso() -> str:
    """当前 UTC 时间，秒级 ISO-8601 带时区偏移，如 2026-08-27T06:30:00+00:00。

    所有 created_at / updated_at / 日志时间戳统一走此函数。
    """
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def utcnow_minus_days(days: int) -> str:
    """当前 UTC 时刻减去 N 天（ISO 带偏移），用于日志保留期 cutoff。"""
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).isoformat(timespec="seconds")


def utc_from_local_midnight(days_ago: int = 0) -> str:
    """本地时区某天 00:00 对应的 UTC 时刻（ISO 带偏移）。

    用于"今日/近N天"类统计口径：按用户本地日切分，而不是 UTC 日。
    """
    local = (_dt.datetime.now().astimezone() - _dt.timedelta(days=days_ago))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(_dt.timezone.utc).isoformat(timespec="seconds")