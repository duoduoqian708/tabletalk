"""敏感名单过滤：按连接级名单剔除表/列，屏蔽项不进发模型的上下文与知识库。

支持两种条目，可混排：
- 字符串（旧名单，glob）：如 "payroll_*"、"*email*" —— 表名与列名按 fnmatch 匹配；
- 精确名结构（新交互，段8）：{"table": "users", "columns": ["email", "phone"]}
  —— 表名精确匹配；columns 缺省/空 = 整表排除；非空 = 仅剔除该表内的对应列。

内嵌列（dialect 适配器内部结构）与顶层列（get_schema 标准结构）都覆盖；
表名过滤同时剔除引用该表的 FK 条目（避免悬空引用）。
"""
from __future__ import annotations

import fnmatch
from typing import Any


def _is_table_removed(entry: str | dict[str, Any], tname: str) -> bool:
    """字符串 → glob 匹配表名；dict → 表名精确匹配（仅当未指定列 = 整表排除）。
    精确名分支大小写归一（与 glob 分支一致）：表名/列名统一小写比较。"""
    if isinstance(entry, str):
        return fnmatch.fnmatch(tname.lower(), entry.lower())
    if str(entry.get("table", "")).lower() != tname.lower():
        return False
    return not (entry.get("columns") or [])  # 列缺省/空 → 整表排除


def _is_column_removed(entry: str | dict[str, Any], tname: str, cname: str) -> bool:
    """字符串 → glob 匹配列名（跨表）；dict → 仅当表名精确命中且列在子集内。"""
    if isinstance(entry, str):
        return fnmatch.fnmatch(cname.lower(), entry.lower())
    if str(entry.get("table", "")).lower() != tname.lower():
        return False
    cols = [str(c).lower() for c in (entry.get("columns") or [])]
    return cname.lower() in cols


def _t_removed(sensitive: list, tname: str) -> bool:
    return any(_is_table_removed(e, tname) for e in sensitive)


def _c_removed(sensitive: list, tname: str, cname: str) -> bool:
    return any(_is_column_removed(e, tname, cname) for e in sensitive)


def filter_sensitive(schema: dict[str, Any], sensitive: list[Any]) -> dict[str, Any]:
    if not sensitive:
        return schema

    # 1. 表级过滤（内嵌列结构也一并剔除列）
    tables = schema.get("tables", [])
    kept = []
    for t in tables:
        tname = t.get("name", "")
        if _t_removed(sensitive, tname):
            continue
        cols = t.get("columns", [])
        kept_cols = [c for c in cols if not _c_removed(sensitive, tname, c.get("name", ""))]
        if len(kept_cols) != len(cols):
            t = {**t, "columns": kept_cols}
        kept.append(t)

    # 2. 顶层列数组过滤：整表剔除联动 + 列级命中
    kept_tables = {t.get("name") for t in kept}
    columns = schema.get("columns", [])
    kept_cols_top = [
        c for c in columns
        if c.get("table") in kept_tables
        and not _c_removed(sensitive, c.get("table", ""), c.get("name", ""))
    ]

    # 3. FK 悬空清理：引用了被剔除表/列的边不再出现在结构中
    fks = schema.get("foreign_keys", [])
    kept_fks = [
        f for f in fks
        if f.get("table") in kept_tables and f.get("ref_table") in kept_tables
        and not _c_removed(sensitive, f.get("table", ""), f.get("column", ""))
        and not _c_removed(sensitive, f.get("ref_table", ""), f.get("ref_column", ""))
    ]

    out = {**schema, "tables": kept, "columns": kept_cols_top}
    if len(kept_fks) != len(fks):
        out["foreign_keys"] = kept_fks
    return out