"""方言感知的 schema 发现、单表描述、DDL 导出、以及给 AI 的 summarize()。

知识库与 AI 上下文都从这里拿"结构"——本模块永不接触行数据。
"""
from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

from app.core.dialects.base import ColumnRef, FKRef, TableRef

if TYPE_CHECKING:
    from app.state import AppState

_CACHE_TTL = 30.0
_schema_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def _discover(state: "AppState", conn_id: str):
    def _work(adapter, conn):
        async def inner():
            tables = await adapter.list_tables(conn)
            columns: list[ColumnRef] = []
            for t in tables:
                columns += await adapter.list_columns(conn, t.name)
            fks = await adapter.list_foreign_keys(conn)
            row_counts = {t.name: await adapter.count_rows(conn, t.name) for t in tables}
            return tables, columns, fks, row_counts

        return inner()

    return await state.pools.run(conn_id, _work)


async def get_schema(state: "AppState", conn_id: str, refresh: bool = False) -> dict[str, Any]:
    cached = _schema_cache.get(conn_id)
    if not refresh and cached and time.monotonic() - cached[0] < _CACHE_TTL:
        return cached[1]

    cfg = state.connections.get(conn_id)
    tables, columns, fks, row_counts = await _discover(state, conn_id)
    fk_set = {(fk.table, fk.column) for fk in fks}
    for c in columns:
        c.is_fk = (c.table, c.name) in fk_set

    result: dict[str, Any] = {
        "connection": cfg.name,
        "dialect": cfg.dialect,
        "databases": [cfg.database] if cfg.database else [],
        "tables": [
            {"name": t.name, "kind": t.kind, "comment": t.comment,
             "column_count": sum(1 for c in columns if c.table == t.name),
             "row_count": row_counts.get(t.name, 0)}
            for t in tables
        ],
        "columns": [
            {"table": c.table, "name": c.name, "type": c.data_type, "nullable": c.nullable,
             "pk": c.is_pk, "fk": c.is_fk, "default": c.default, "comment": c.comment}
            for c in columns
        ],
        "foreign_keys": [
            {"table": fk.table, "column": fk.column, "ref_table": fk.ref_table,
             "ref_column": fk.ref_column, "constraint_id": fk.constraint_id}
            for fk in fks
        ],
    }
    _schema_cache[conn_id] = (time.monotonic(), result)
    return result


def invalidate_schema(conn_id: str) -> None:
    _schema_cache.pop(conn_id, None)


async def describe_table(state: "AppState", conn_id: str, table: str) -> dict[str, Any]:
    schema = await get_schema(state, conn_id)
    tables = {t["name"]: t for t in schema["tables"]}
    tinfo = tables.get(table)
    if tinfo is None:
        raise KeyError(f"表不存在: {table}")
    columns = [c for c in schema["columns"] if c["table"] == table]
    return {"name": table, "kind": tinfo["kind"], "comment": tinfo["comment"], "columns": columns}


async def preview_table(state: "AppState", conn_id: str, table: str, limit: int = 100) -> dict[str, Any]:
    def _work(adapter_obj, conn):
        async def inner():
            quote = adapter_obj.quote_ident
            sel = await adapter_obj.execute(conn, f"SELECT * FROM {quote(table)} LIMIT {limit}")
            cnt = await adapter_obj.execute(conn, f"SELECT COUNT(*) AS _n FROM {quote(table)}")
            total = cnt.rows[0][0] if cnt.rows else 0
            return sel, total

        return inner()

    from app.core.query import serialize_rows

    raw, total = await state.pools.run(conn_id, _work)
    columns, rows, types = serialize_rows(raw, max_rows=limit)
    return {"columns": columns, "types": types, "rows": rows, "total": total}


def _looks_like_enum(col: Any) -> bool:
    """枚举列启发式判定（T5 采样分列）：

    信号（满足任一即倾向枚举）：
    - 类型 bool/boolean
    - 列名含 status/type/kind/state/category/flag/is_
    - 类型为短 char/varchar（长度 ≤ 20）
    反信号（度量/时间列，直接排除）：
    - 列名含 _at/_time/date/amount/price/qty/count/num
    """
    name = (getattr(col, "name", "") or "").lower()
    ctype = (getattr(col, "data_type", "") or "").upper()
    if name.endswith(("_at", "_time")) or "date" in name or "time" in name:
        return False
    if any(tok in name for tok in ("amount", "price", "qty", "count", "num")):
        return False
    if ctype.startswith(("BOOL", "BOOLEAN")):
        return True
    if name.startswith("is_") or name in ("deleted", "active", "enabled", "valid"):
        return True  # 布尔标志列（is_deleted/is_active...）
    if any(tok in name for tok in ("status", "type", "kind", "state", "category", "flag")):
        return True
    if ctype.startswith(("CHAR", "VARCHAR", "NCHAR", "NVARCHAR")):
        # 短 char/varchar（length ≤ 20）倾向枚举；带长度且 >20（如 VARCHAR(255)）更可能是
        # ID/描述，走最近行路径；裸类型（无长度信息）保守判枚举（DISTINCT 对 ID 类无害，T5 §4#5）
        m = re.search(r"\((\d+)\)", ctype)
        return m is None or int(m.group(1)) <= 20
    return False


async def sample_values(state: "AppState", conn_id: str, table: str, per_column: int = 10) -> dict[str, list[Any]]:
    """分列抽样（T5）：枚举/低基数列走 SELECT DISTINCT（不按时间偏置，覆盖历史值），
    度量/时间列走整行主键倒序（最近样本）。返回 {列名: [该列值]}。

    供图谱值重叠边（T4）、AI 注释、概念字典防漂移（T7）使用。
    """
    import logging

    logger = logging.getLogger("core.schema")
    from app.core.query import serialize_value

    def _work(adapter, conn):
        async def inner():
            quote = adapter.quote_ident
            cols = await adapter.list_columns(conn, table)
            enum_cols = [c.name for c in cols if _looks_like_enum(c)]
            pk_cols = [c.name for c in cols if getattr(c, "is_pk", False)]

            out: dict[str, list[Any]] = {}

            # R4：小表（行数 ≤ 50）全表 DISTINCT（不按时间偏置，覆盖全部历史值）；
            # 行数未知（count 失败）→ 跳过该信号，保持既有分列策略
            small_table = False
            try:
                n_rows = await adapter.count_rows(conn, table)
                small_table = n_rows <= 50
            except Exception:  # pragma: no cover - count 失败退化为分列采样
                small_table = False

            # 1. 枚举列：SELECT DISTINCT（历史值不被时间偏置吞掉；NULL 剔除，T4 消费层同语义）
            for cname in enum_cols:
                try:
                    raw = await adapter.execute(
                        conn, f"SELECT DISTINCT {quote(cname)} FROM {quote(table)} LIMIT {int(per_column)}"
                    )
                    out[cname] = [serialize_value(r[0]) for r in raw.rows or [] if r and r[0] is not None]
                except Exception as e:  # pragma: no cover - 方言兼容
                    logger.warning("[schema.sample] conn=%s table=%s DISTINCT 采样失败 %s：%s",
                                   conn_id, table, cname, e)
                    out[cname] = []

            if small_table:
                # R4：小表全表 DISTINCT——所有非枚举列同样取全量去重值（不限行，行数本身 ≤50）
                for c in cols:
                    cname = c.name
                    if cname in enum_cols:
                        continue
                    try:
                        raw = await adapter.execute(
                            conn, f"SELECT DISTINCT {quote(cname)} FROM {quote(table)}"
                        )
                        out[cname] = [serialize_value(r[0]) for r in raw.rows or [] if r and r[0] is not None]
                    except Exception as e:  # pragma: no cover - 方言兼容
                        logger.warning("[schema.sample] conn=%s table=%s 小表 DISTINCT 失败 %s：%s",
                                       conn_id, table, cname, e)
                        out[cname] = []
                logger.debug("[schema.sample] conn=%s table=%s 小表全表 DISTINCT（%d 列）",
                             conn_id, table, len(cols))
            else:
                # 2. 度量/时间/其他列：整行主键倒序（最近样本）——仅大表路径
                order = ""
                if pk_cols:
                    order = " ORDER BY " + ", ".join(f"{quote(c)} DESC" for c in pk_cols)
                try:
                    raw = await adapter.execute(
                        conn, f"SELECT * FROM {quote(table)}{order} LIMIT {int(per_column)}"
                    )
                except Exception as e:  # T5 §4#6：采样失败返回 {}（已采到的枚举值保留），不炸构建
                    logger.warning("[schema.sample] conn=%s table=%s 采样失败：%s", conn_id, table, e)
                    return out
                names = list(raw.columns) if raw.columns else [c.name for c in cols]
                for n in names:
                    out.setdefault(n, [])
                for row in raw.rows or []:
                    for n, v in zip(names, row):
                        if n not in enum_cols:
                            out[n].append(serialize_value(v))

            # 3. 判别器分组采样（R2/§3.4）：低基数 type/_type/kind + 引用列（_id/_code 后缀）
            #    → out["_grouped"] = {"type": {"__ref__": "ref_id", 1: [...], 2: [...]}}
            try:
                disc_cols = [c.name for c in cols
                             if c.name.lower() in ("type", "_type", "kind") and c.name not in pk_cols]
                if disc_cols:
                    ref_cols = [c.name for c in cols
                                if (c.name.lower().endswith("_id") or c.name.lower().endswith("_code"))
                                and c.name not in pk_cols]
                    if ref_cols:
                        disc = disc_cols[0]
                        ref = ref_cols[0]
                        raw_g = await adapter.execute(
                            conn, f"SELECT {quote(disc)}, {quote(ref)} FROM {quote(table)}"
                                 f" LIMIT {int(per_column) * 10}"
                        )
                        groups: dict[str, Any] = {"__ref__": ref}
                        if raw_g.rows:
                            for row in raw_g.rows:
                                if len(row) < 2:
                                    continue
                                dval, rval = row[0], row[1]
                                gkey = serialize_value(dval) if dval is not None else None
                                if gkey is None:
                                    continue
                                lst = groups.setdefault(gkey, [])
                                if len(lst) < int(per_column):
                                    lst.append(serialize_value(rval))
                            if len([k for k in groups if k != "__ref__"]) <= 20:
                                out["_grouped"] = {disc: groups}
            except Exception as e:  # pragma: no cover - 分组采样失败静默降级
                logger.debug("[schema.sample] conn=%s table=%s 判别器分组采样失败：%s",
                             conn_id, table, e)

            logger.debug("[schema.sample] conn=%s table=%s enum_cols=%d metric_cols=%d total=%d",
                         conn_id, table, len(enum_cols), len(cols) - len(enum_cols), len(cols))
            return out

        return inner()

    return await state.pools.run(conn_id, _work)


async def export_ddl(state: "AppState", conn_id: str, table: str) -> dict[str, Any]:
    cfg = state.connections.get(conn_id)

    def _work(adapter, conn):
        async def inner():
            quote = adapter.quote_ident
            literal = adapter.quote_literal
            tables = await adapter.list_tables(conn)
            tab = next((t for t in tables if t.name == table), None)
            if tab is None:
                raise KeyError(f"表不存在: {table}")
            cols = await adapter.list_columns(conn, table)
            fks = [f for f in await adapter.list_foreign_keys(conn) if f.table == table]
            lines = [f"CREATE TABLE {quote(table)} ("]
            defs = []
            for c in cols:
                parts = [quote(c.name), c.data_type]
                if c.is_pk:
                    parts.append("PRIMARY KEY")
                elif c.nullable is False:
                    parts.append("NOT NULL")
                if c.default is not None:
                    parts.append(f"DEFAULT {literal(c.default)}")
                defs.append("  " + " ".join(parts))
            for fk in fks:
                defs.append(
                    f"  CONSTRAINT fk_{table}_{fk.column} FOREIGN KEY ({quote(fk.column)}) "
                    f"REFERENCES {quote(fk.ref_table)} ({quote(fk.ref_column)})"
                )
            lines.append(",\n".join(defs))
            lines.append(");")
            return {"table": table, "ddl": "\n".join(lines)}

        return inner()

    return await state.pools.run(conn_id, _work)


def summarize(schema: dict[str, Any], table: str | None = None, table_names: list[str] | None = None) -> str:
    """给 AI 的 schema 摘要：只发结构，不含行数据。

    table 过滤单表；table_names 过滤一组表（领域路由后的候选子图）。
    """
    parts: list[str] = []
    tables = {t["name"]: t for t in schema["tables"]}
    columns = {t: [] for t in tables}
    for c in schema["columns"]:
        if c["table"] in columns:
            columns[c["table"]].append(c)

    for tname, tinfo in tables.items():
        if table and tname != table:
            continue
        if table_names and tname not in table_names:
            continue
        tag = "view" if tinfo["kind"] == "view" else "table"
        head = f"- {tname} ({tag}"
        if tinfo.get("column_count"):
            head += f", {tinfo['column_count']} 列"
        if tinfo.get("comment"):
            head += f", 注释: {tinfo['comment']}"
        parts.append(head + ")")
        for c in columns.get(tname, []):
            marks = []
            if c["pk"]:
                marks.append("PK")
            if c["fk"]:
                marks.append("FK")
            mark = (" " + "/".join(marks)) if marks else ""
            extra = f" 默认 {c['default']}" if c.get("default") else ""
            cmt = f"  // {c['comment']}" if c.get("comment") else ""
            parts.append(f"  - {c['name']}: {c['type']}{mark}{extra}{cmt}")

    for fk in schema.get("foreign_keys", []):
        if table and fk["table"] != table:
            continue
        if table_names and (fk["table"] not in table_names or fk["ref_table"] not in table_names):
            continue
        parts.append(f"- FK {fk['table']}.{fk['column']} → {fk['ref_table']}.{fk['ref_column']}")

    return "\n".join(parts)
