"""敏感名单过滤：按连接级 glob 剔除表/列，屏蔽项不进发模型的上下文与知识库。"""
from __future__ import annotations

import fnmatch
from typing import Any


def _match_any(patterns: list[str], name: str) -> bool:
    return any(fnmatch.fnmatch(name.lower(), p.lower()) for p in patterns)


def filter_sensitive(schema: dict[str, Any], patterns: list[str]) -> dict[str, Any]:
    if not patterns:
        return schema
    tables = schema.get("tables", [])
    kept = []
    for t in tables:
        tname = t.get("name", "")
        if _match_any(patterns, tname):
            continue
        cols = t.get("columns", [])
        kept_cols = [c for c in cols if not _match_any(patterns, c.get("name", ""))]
        if len(kept_cols) != len(cols):
            t = {**t, "columns": kept_cols}
        kept.append(t)
    return {**schema, "tables": kept}
