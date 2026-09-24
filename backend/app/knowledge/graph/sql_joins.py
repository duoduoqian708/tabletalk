"""SQL join 解析（共享）：图校验器（plausibility gate, §10⑤）与日志挖掘（T10）共用。

extract_join_pairs：sqlglot 解析 SQL 的 JOIN 等值列对 → [(from_t, from_c, to_t, to_c)]。
纯函数、无 I/O；解析失败/非等值/无表限定 → 跳过（宁缺勿错）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("kb.graph.sql_joins")


def extract_join_pairs(sql: str) -> list[tuple[str, str, str, str]]:
    """提取 SQL 中所有 JOIN 的等值列对（已做别名 → 真实表名映射）。

    - 只处理简单表 join（非子查询）
    - 只提取 EQ 等值条件（范围 join 等非等值跳过）
    - 列无表限定无法归属 → 跳过该条件
    """
    import sqlglot

    if not sql or not sql.strip():
        return []
    try:
        expr = sqlglot.parse_one(sql)
    except Exception:
        logger.debug("[sql_joins] 解析失败，跳过：%s", sql[:80])
        return []
    try:
        # 别名 → 真实表名映射（orders o → o: orders）
        aliases: dict[str, str] = {}
        for t in expr.find_all(sqlglot.exp.Table):
            name = t.name or ""
            aliases[t.alias or name] = name
        out: list[tuple[str, str, str, str]] = []
        for join in expr.find_all(sqlglot.exp.Join):
            this = join.this
            if not isinstance(this, sqlglot.exp.Table):
                continue  # 子查询 join：跳过
            to_t = this.name
            on = join.args.get("on")
            if on is None:
                continue
            for eq in on.find_all(sqlglot.exp.EQ):
                l, r = eq.this, eq.expression
                if not isinstance(l, sqlglot.exp.Column) or not isinstance(r, sqlglot.exp.Column):
                    continue
                lc, rc = l.name, r.name
                l_tbl, r_tbl = l.table, r.table
                if not lc or not rc:
                    continue
                l_tbl = aliases.get(l_tbl, l_tbl)
                r_tbl = aliases.get(r_tbl, r_tbl)
                if l_tbl and not r_tbl:
                    out.append((l_tbl, lc, to_t, rc))
                elif r_tbl and not l_tbl:
                    out.append((to_t, lc, r_tbl, rc))
                elif l_tbl and r_tbl:
                    # 保证方向稳定：按表名排序归一（t1 恒为字典序小者），图校验双向匹配
                    if l_tbl < r_tbl:
                        out.append((l_tbl, lc, r_tbl, rc))
                    else:
                        out.append((r_tbl, rc, l_tbl, lc))
                else:
                    continue  # 两侧无表限定：无法归属
        return out
    except Exception as e:  # pragma: no cover
        logger.debug("[sql_joins] join 解析异常 %s：%s", e, sql[:80])
        return []


__all__ = ["extract_join_pairs"]