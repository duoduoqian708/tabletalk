"""敏感名单过滤：按连接级 glob 剔除表/列，屏蔽项不进发模型的上下文与知识库。

支持两种结构：
- 内嵌列（dialect 适配器内部结构）：tables[].columns
- 顶层列（get_schema 标准结构）：schema["columns"] 独立数组
表名过滤同时剔除引用该表的 FK 条目（避免悬空引用）。
"""
from __future__ import annotations

import fnmatch
from typing import Any


def _match_any(patterns: list[str], name: str) -> bool:
    return any(fnmatch.fnmatch(name.lower(), p.lower()) for p in patterns)


def filter_sensitive(schema: dict[str, Any], patterns: list[str]) -> dict[str, Any]:
    if not patterns:
        return schema

    # 1. 表级过滤（内嵌列结构也一并剔除列）
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

    # 2. 顶层列数组过滤：列名 glob 命中，或被剔除表的列（联动表级名单）
    kept_tables = {t.get("name") for t in kept}
    columns = schema.get("columns", [])
    kept_cols_top = [
        c for c in columns
        if c.get("table") in kept_tables and not _match_any(patterns, c.get("name", ""))
    ]

    # 3. FK 悬空清理：引用了被剔除表/列的边不再出现在结构中
    fks = schema.get("foreign_keys", [])
    kept_tables = {t.get("name") for t in kept}
    kept_fks = [
        f for f in fks
        if f.get("table") in kept_tables and f.get("ref_table") in kept_tables
        and not _match_any(patterns, f.get("column", ""))
        and not _match_any(patterns, f.get("ref_column", ""))
    ]

    out = {**schema, "tables": kept, "columns": kept_cols_top}
    if len(kept_fks) != len(fks):
        out["foreign_keys"] = kept_fks
    return out
