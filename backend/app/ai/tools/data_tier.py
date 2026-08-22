"""三档行数据处理（B4 隐私档：standard/open/strict）共享函数。

严格档绝不返回行数据（只回列名+行数）；标准档 rows 出网前 redact_rows（本地确定性 token，fail-closed）；
开放档明文。run_query（tools/sql.py）与 load_result 共用此逻辑，避免复制。
"""
from __future__ import annotations

from typing import Any


def privacy_mode(state) -> str:
    try:
        return state.runtime.get().privacy_mode
    except Exception:
        return "standard"


def apply_data_tier(state, conn_id: str, rows: list | None, columns: list[str]) -> tuple[list | None, list[str]]:
    """按当前隐私档处理行数据。返回 (rows_or_None, redactions)。

    - strict：rows=None（行数据一律不返回，仅列名+行数）。
    - open：明细原样。
    - standard：redact_rows；脱敏异常 fail-closed 丢弃行而非明文直发。
    """
    mode = privacy_mode(state)
    if mode == "strict":
        return None, []
    if mode == "open":
        return rows, []
    if not rows:
        return rows, []
    try:
        from app.safety.redact import get_salt, redact_rows
        from app.config import get_env

        salt = get_salt(get_env().data_dir)
        try:
            sensitive = state.connections.get(conn_id).sensitive
        except Exception:
            sensitive = []
        redacted, mp = redact_rows(rows, columns, "", salt, sensitive)
        return redacted, list(mp.keys())[:5]
    except Exception:
        # B2 fail-closed：脱敏异常时丢弃行数据而非明文直发
        return [], ["redact-failed"]


def codify_columns_for_model(state, conn_id: str, columns: list[str]) -> list[str]:
    """B3 回喂代号化：列名按敏感表代号化（表元数据缺失时尽力，未命中保持原文）。"""
    try:
        from app.ai.tools.sql import _codify_columns_for_model

        return _codify_columns_for_model(state, conn_id, columns, "")
    except Exception:
        return columns