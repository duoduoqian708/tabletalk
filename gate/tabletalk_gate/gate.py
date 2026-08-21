"""安全闸门编排：assess（纯逻辑）+ preview（DB 预估影响行数）。

闸门本地运行、模型无关——模型离线也照常拦截。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp

from . import parser, rules
from .models import Assessment, Origin

if TYPE_CHECKING:
    from app.state import AppState


def assess_sql(sql: str, sqlglot_dialect: str, origin: Origin) -> Assessment:
    infos = parser.parse_sql(sql, sqlglot_dialect)
    results = rules.run_rules(infos, origin)
    agg = rules.aggregate(infos, results)
    return Assessment(
        verdict=agg["verdict"],
        tier=agg["tier"],
        reasons=agg["reasons"],
        rules=agg["rules"],
        tables=agg["tables"],
        has_where=agg["has_where"],
        has_limit=agg["has_limit"],
        parse_error=agg["parse_error"],
        is_multi=agg["is_multi"],
    )


def preview_count_query(sql: str, sqlglot_dialect: str) -> str | None:
    """为 UPDATE/DELETE 构造同 WHERE 的 COUNT 查询。"""
    try:
        parsed = sqlglot.parse(sql, read=sqlglot_dialect)
    except Exception:
        return None
    for node in parsed:
        if node is None:
            continue
        if isinstance(node, (exp.Update, exp.Delete)):
            tgt = node.args.get("this")
            where = node.args.get("where")
            if tgt is None or where is None:
                return None
            table_sql = tgt.sql(dialect=sqlglot_dialect)
            cond = where.this.sql(dialect=sqlglot_dialect)
            return f"SELECT COUNT(*) AS _n FROM {table_sql} WHERE {cond}"
    return None


async def preview_rows(state: "AppState", conn_id: str, sql: str, sqlglot_dialect: str) -> int | None:
    q = preview_count_query(sql, sqlglot_dialect)
    if q is None:
        return None
    # COUNT 在亿级表上可能很慢：超时/失败则返回 None，由 UI 显示"无法预估，请人工核对"
    try:
        raw = await asyncio.wait_for(
            state.pools.execute(conn_id, q),
            timeout=state.env.gate_preview_timeout,
        )
    except Exception:  # noqa: BLE001  超时或执行失败 → 无法预估
        return None
    if raw.rows:
        try:
            return int(raw.rows[0][0])
        except (TypeError, ValueError):
            return None
    return None


def suggest_safe(sql: str, sqlglot_dialect: str, origin: Origin) -> list[str]:
    assessment = assess_sql(sql, sqlglot_dialect, origin)
    suggests: list[str] = []
    for r in assessment.rules:
        if r.rule == "dml-no-where":
            suggests.append("给语句加上 WHERE 条件，只影响目标行。")
            suggests.append("先用 SELECT COUNT(*) 确认目标行数，再写 UPDATE/DELETE。")
        elif r.rule == "ddl-ai":
            suggests.append("把脚本发送到编辑器，由你手动执行（DDL 仅手动）。")
        elif r.rule == "multi-statement":
            suggests.append("拆成单条语句逐条执行。")
        elif r.rule == "parse-failure":
            suggests.append("检查 SQL 语法，或拆成单条语句。")
    return suggests or [r.get("message", "") for r in assessment.reasons[:1]]


def sqlglot_dialect_for(dialect: str) -> str:
    return dialect or "sqlite"