"""临时排查日志 helper（TODO: 测试完成/验收通过后 DELETED 此文件与所有 import）。

用标准 logging 打到 root logger，uvicorn 默认会显示到控制台。CLEARED_DEBUGLOG=1 时输出；
缺省也输出（便于排查），正式交付前整个删除。
"""
from __future__ import annotations

import contextlib
import os
import sys
import time


# TODO: 删除（测试排查专用）
def _enabled() -> bool:
    return os.environ.get("CLEARED_DEBUGLOG", "1") == "1"


# TODO: 删除（测试排查专用）
def dbg(*parts) -> None:
    """统一前置 [CLEARED] 标记 + 毫秒时间戳，便于 grep。"""
    if not _enabled():
        return
    ts = time.strftime("%H:%M:%S")
    print(f"[CLEARED] {ts} {' '.join(str(p) for p in parts)}", file=sys.stderr, flush=True)


# TODO: 删除（测试排查专用）
@contextlib.contextmanager
def dbg_timer(step: str):
    """步耗时计时，如 dbg_timer('assemble_context')。"""
    t0 = time.monotonic()
    try:
        yield
    finally:
        dbg(f"[timer] {step} = {round((time.monotonic() - t0) * 1000, 1)}ms")
