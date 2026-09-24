"""轻量 5 字段 cron 解析/计算（无第三方依赖）。

字段顺序: 分 时 日 月 周(0-7，0/7=周日，1=周一)。
每字段支持 `*` / `*/N` / `A-B` / `A,B,C` 混合。日/周同时受限时按 AND 处理（常见近似）。
next_after 逐分钟探测（上限 5 年），免依赖且够用。
"""
from __future__ import annotations

import datetime as _dt

_FIELD_LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


def _parse_field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    spec = (spec or "").strip()
    if not spec:
        return out
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            out |= set(range(lo, hi + 1))
        elif "/" in part:
            base, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                continue
            if step < 1:
                continue
            if base == "*" or base == "":
                low, high = lo, hi
            elif "-" in base:
                a, b = base.split("-", 1)
                low, high = int(a), int(b)
            else:
                low = high = int(base)
            out |= {v for v in range(low, high + 1) if v % step == 0}
        elif "-" in part:
            a, b = part.split("-", 1)
            try:
                out |= set(range(int(a), int(b) + 1))
            except ValueError:
                continue
        else:
            try:
                v = int(part)
            except ValueError:
                continue
            if lo == 0 and hi == 6 and v == 7:  # 周日 7 == 0
                v = 0
            if lo <= v <= hi:
                out.add(v)
    return out


def parse(cron: str) -> list[set[int]] | None:
    """解析为 5 个允许集合；非法/不满足字段数 → None。"""
    parts = (cron or "").split()
    if len(parts) != 5:
        return None
    fields: list[set[int]] = []
    for i, p in enumerate(parts):
        lo, hi = _FIELD_LIMITS[i]
        f = _parse_field(p, lo, hi)
        if not f:
            return None
        fields.append(f)
    return fields


def _dow_matches(cron_dow: set[int], dt: _dt.datetime) -> bool:
    return ((dt.weekday() + 1) % 7) in cron_dow  # python(mon=0) → cron(mon=1, sun=0)


def is_due(cron: str, now: _dt.datetime) -> bool:
    f = parse(cron)
    if f is None:
        return False
    fmin, fhour, fdom, fmon, fdow = f
    return (
        now.minute in fmin
        and now.hour in fhour
        and now.day in fdom
        and now.month in fmon
        and _dow_matches(fdow, now)
    )


def next_after(cron: str, dt: _dt.datetime) -> _dt.datetime | None:
    """返回 dt 之后的首次触发时刻（不含 dt 本身）；无下一个（out of 5yr）→ None。"""
    f = parse(cron)
    if f is None:
        return None
    fmin, fhour, fdom, fmon, fdow = f
    d = dt.replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
    end = dt + _dt.timedelta(days=5 * 365)
    while d <= end:
        if (
            d.month in fmon
            and d.day in fdom
            and _dow_matches(fdow, d)
            and d.hour in fhour
            and d.minute in fmin
        ):
            return d
        d += _dt.timedelta(minutes=1)
    return None


_DOW_CN = ("日", "一", "二", "三", "四", "五", "六")


def friendly(cron: str) -> str:
    """人类可读（常见 5 段格式；其余回原文）。"""
    c = (cron or "").strip().split()
    if len(c) != 5:
        return cron or ""
    min_s, hour_s, _dom, _mon, dow_s = c
    if min_s == "*/N" or (min_s.startswith("*/") and hour_s == "*" and dow_s == "*"):
        try:
            n = int(min_s.split("/")[1])
            if n == 30:
                return "每 30 分钟"
            if n == 5:
                return "每 5 分钟"
            return f"每 {n} 分钟"
        except ValueError:
            pass
    try:
        hh = int(hour_s)
        mm = int(min_s)
    except ValueError:
        return cron
    if dow_s == "*":
        return f"每天 {hh:02d}:{mm:02d}"
    try:
        d = int(dow_s.split("/")[0])
        if "/" in dow_s:  # 按周的那几天
            return f"每周{dow_s} {hh:02d}:{mm:02d}"
        return f"每周{_DOW_CN[d % 7]} {hh:02d}:{mm:02d}"
    except ValueError:
        return cron