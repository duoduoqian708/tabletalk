"""建边管线（纯函数、无 LLM、无 I/O）。

2026-08-31 简化：确定性来源只保留 FK + 命名推断（设计 §6 修订），
产出统一 draft 边（待人工确认，不确认不生效）：
- FK=1.0，命名=0.6；值重叠/判别器采样检测已删（复杂度高、误报空间大，
  非 FK 关系交由 LLM 识别提案）
"""
from __future__ import annotations

import logging
from typing import Any

from app.knowledge.graph.model import (
    PROV_DECLARED_FK,
    PROV_NAMING_INFERENCE,
    SOURCE_FK,
    SOURCE_NAMING,
    GraphEdge,
)

logger = logging.getLogger("kb.graph_builder")

_REF_SUFFIXES = ("_id", "_code", "_no", "_num", "_key", "_ref")

# 通用名（不做匹配锚点）
_GENERIC_NAMES = {
    "id", "uuid", "guid", "status", "type", "name", "code",
    "created_at", "updated_at", "deleted_at", "created_by", "updated_by",
}


def _table_name_variants(base: str) -> set[str]:
    """表名单复数变体：category_id → {category, categories}；addresses_id → {addresses, address}。"""
    out = {base}
    if base.endswith("ies"):
        out.add(base[:-3] + "y")      # categories → category
    elif base.endswith("es"):
        out.add(base[:-2])            # addresses → address
    elif base.endswith("y"):
        out.add(base[:-1] + "ies")    # category → categories
    elif base.endswith("s"):
        out.add(base[:-1])            # status → statu（粗糙单数化，匹配不到则无害）
    out.add(base + "s")
    out.add(base + "es")
    return out


def _type_family(t: str | None) -> str | None:
    """类型族：INT 族 / 文本族；未知返回 None（保守：不参与候选）。"""
    u = (t or "").upper()
    if u.startswith(("INT", "BIGINT", "SMALLINT", "TINYINT", "MEDIUMINT", "NUMERIC", "DECIMAL")):
        return "int"
    if u.startswith(("CHAR", "VARCHAR", "TEXT", "NCHAR", "NVARCHAR", "STRING")):
        return "text"
    return None


# ---------------------------------------------------------------- FK

def build_fk_edges(schema: dict) -> list[GraphEdge]:
    """声明 FK → source=fk, confidence=1.0, provenance=declared_fk。

    复合 FK（同 constraint_id 多列对）→ 合并为一条边、cols 多对列；
    平行 FK（无 constraint_id 或约束不同）→ 每条独立边（平行边正确性优先）。
    """
    col_pk = {(c["table"], c["name"]): bool(c.get("pk")) for c in schema.get("columns", [])}
    # 按 (table, ref_table, constraint_id) 分组；constraint_id 缺失 → 每条独立组
    groups: dict[tuple, list[dict]] = {}
    for i, fk in enumerate(schema.get("foreign_keys", [])):
        cid = fk.get("constraint_id")
        key = (fk["table"], fk["ref_table"], cid if cid is not None else ("__uniq__", i))
        groups.setdefault(key, []).append(fk)
    edges: list[GraphEdge] = []
    for (from_t, to_t, _cid), fks in groups.items():
        col_pairs = [(fk["column"], fk.get("ref_column") or "id") for fk in fks]
        edges.append(GraphEdge(
            source_table=from_t, target_table=to_t,
            cols=col_pairs,
            cardinality="1:1" if col_pk.get((from_t, col_pairs[0][0])) else "n:1",
            source=SOURCE_FK, confidence=1.0, provenance=PROV_DECLARED_FK,
            reason=f"FK 约束：{from_t}.{','.join(c for c, _ in col_pairs)} → {to_t}",
        ))
    logger.info("[graph_builder] fk 产边 %d 条", len(edges))
    return edges


# ---------------------------------------------------------------- 命名推断

def build_naming_edges(schema: dict) -> list[GraphEdge]:
    """命名推断 → source=naming, confidence=0.6, provenance=naming_inference。


    规则：引用列名（_id/_code/... 后缀）去掉后缀 ≈ 目标表名（单复数变体），
    且类型族一致；目标列主键优先，其次 id/code。
    """
    tables = schema.get("tables", [])
    columns = schema.get("columns", [])
    cols_by_table: dict[str, list[dict]] = {}
    for c in columns:
        cols_by_table.setdefault(c["table"], []).append(c)
    table_names = {t["name"] for t in tables}
    table_lower = {n.lower(): n for n in table_names}

    existing: set[tuple[str, str, str, str]] = set()  # (from_t, from_c, to_t, to_c)
    edges: list[GraphEdge] = []

    def _already(from_t, from_c, to_t, to_c) -> bool:
        key = (from_t, from_c, to_t, to_c)
        if key in existing:
            return True
        existing.add(key)
        return False

    for t in tables:
        tname = t["name"]
        for c in cols_by_table.get(tname, []):
            cname = c["name"].lower()
            if cname in _GENERIC_NAMES:
                logger.debug("[graph_builder] naming 跳过 %s.%s：通用名排除", tname, c["name"])
                continue
            base = cname
            matched = False
            for sfx in _REF_SUFFIXES:
                if cname.endswith(sfx):
                    base = cname[: -len(sfx)]
                    matched = True
                    break
            if not matched or not base or base in _GENERIC_NAMES:
                logger.debug("[graph_builder] naming 跳过 %s.%s：无引用后缀或泛型基名", tname, c["name"])
                continue
            if base == tname.lower():
                logger.debug("[graph_builder] naming 跳过 %s.%s：自环", tname, c["name"])
                continue
            variants = _table_name_variants(base)
            to_t = next((table_lower[v] for v in variants if v in table_lower), None)
            if to_t is None or to_t == tname:
                logger.debug("[graph_builder] naming 跳过 %s.%s：目标表 %s 不存在", tname, c["name"], base)
                continue
            fam = _type_family(c.get("type", ""))
            if fam is None:
                logger.debug("[graph_builder] naming 跳过 %s.%s：类型族未知", tname, c["name"])
                continue
            ref_col: str | None = None
            for cc in sorted(
                cols_by_table.get(to_t, []),
                key=lambda x: (0 if x.get("pk") else 1, 0 if x["name"].lower() in ("id", "code") else 1),
            ):
                if _type_family(cc.get("type", "")) == fam:
                    ref_col = cc["name"]
                    break
            if ref_col is None:
                continue
            if _already(tname, c["name"], to_t, ref_col):
                continue
            edges.append(GraphEdge(
                source_table=tname, target_table=to_t,
                cols=[(c["name"], ref_col)],
                cardinality="1:1" if c.get("pk") else "n:1",
                source=SOURCE_NAMING, confidence=0.6, provenance=PROV_NAMING_INFERENCE,
                reason=f"列名匹配：{tname}.{c['name']} → {to_t}.{ref_col}",
            ))
    logger.info("[graph_builder] naming 产边 %d 条", len(edges))
    return edges


# ---------------------------------------------------------------- 值重叠

# ---------------------------------------------------------------- 查询日志（委托 T10）

def build_query_log_edges(audit_rows: list[dict], schema: dict | None = None) -> list[GraphEdge]:
    """查询日志挖掘 → 委托 T10 behavior.log_mining.mine_join_edges。

    schema 提供时：挖掘按 schema 校验（表/列不存在即丢弃，T10 §4#4）。
    T10 未实现时返回 []（调用方静默降级）。
    """
    try:
        from app.knowledge.behavior.log_mining import mine_join_edges  # type: ignore[import-not-found]
        return mine_join_edges(audit_rows, schema=schema)
    except ImportError:
        logger.debug("[graph_builder] behavior.log_mining 未实现（T10），query_log 边跳过")
        return []


# ---------------------------------------------------------------- 统一 draft 产出

def build_draft_edges(schema: dict) -> list[dict]:
    """确定性来源（FK + 命名推断）-> 统一 draft 边列表（待人工确认才生效）。

    跨源去重：同列对 confidence 严格更高者胜，并列时 fk > naming。
    格式与 LLM draft 边一致（from_table/from_col/source/...），供审查页统一展示。
    """
    by_key: dict[tuple, tuple[int, float, Any]] = {}
    for prio, edges in ((0, build_fk_edges(schema)), (1, build_naming_edges(schema))):
        for e in edges:
            key = tuple(sorted((e.source_table, sc, e.target_table, tc)
                               for sc, tc in e.cols)) + (e.guard or "",)
            prev = by_key.get(key)
            if prev is None or e.confidence > prev[1]:
                by_key[key] = (prio, e.confidence, e)
    out: list[dict] = []
    for _, _, e in by_key.values():
        first = e.cols[0]
        out.append({
            "from_table": e.source_table, "from_col": first[0],
            "to_table": e.target_table, "to_col": first[1],
            "source": e.source, "cardinality": e.cardinality,
            "reason": e.reason, "guard": e.guard,
            "confidence": e.confidence, "provenance": e.provenance,
            "cols": [list(p) for p in e.cols],
        })
    logger.info("[graph_builder] 确定性 draft 边 %d 条（fk/naming）", len(out))
    return out
