"""cron 解析/计算 单测。"""
from __future__ import annotations

import datetime as dt

from app.tasks import cron


def test_is_due_basic():
    assert cron.is_due("0 9 * * *", dt.datetime(2026, 9, 1, 9, 0, 0))
    assert not cron.is_due("0 9 * * *", dt.datetime(2026, 9, 1, 10, 0, 0))


def test_next_daily():
    now = dt.datetime(2026, 9, 1, 10, 0, 0)
    assert cron.next_after("0 9 * * *", now) == dt.datetime(2026, 9, 2, 9, 0, 0)


def test_next_every_15min():
    now = dt.datetime(2026, 9, 1, 10, 3, 0)
    assert cron.next_after("*/15 * * * *", now) == dt.datetime(2026, 9, 1, 10, 15, 0)


def test_next_weekly_monday():
    # 2026-08-31 是周一
    n = cron.next_after("0 10 * * 1", dt.datetime(2026, 8, 31, 10, 0, 0))
    assert n == dt.datetime(2026, 9, 7, 10, 0, 0)


def test_dow_7_is_sunday():
    # cron 周 7 == 0；2026-09-06 是周日
    assert cron.is_due("0 0 * * 7", dt.datetime(2026, 9, 6, 0, 0, 0))
    assert cron.is_due("0 0 * * 0", dt.datetime(2026, 9, 6, 0, 0, 0))


def test_invalid_cron():
    assert cron.parse("bad") is None
    assert cron.parse("0 9 * *") is None  # 4 字段
    assert cron.next_after("70 9 * * *", dt.datetime.now()) is None  # 分超界 → 空集
    assert not cron.is_due("not-cron", dt.datetime.now())


def test_friendly():
    assert cron.friendly("0 9 * * *") == "每天 09:00"
    assert cron.friendly("*/30 * * * *") == "每 30 分钟"
    assert cron.friendly("*/5 * * * *") == "每 5 分钟"
    assert cron.friendly("0 10 * * 1") == "每周一 10:00"
    assert cron.friendly("bad") == "bad"
    assert cron.friendly("0 10 * * *") == "每天 10:00"