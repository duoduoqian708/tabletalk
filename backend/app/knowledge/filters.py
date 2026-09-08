"""表级过滤器 + 会话变量池（T8）。

对齐设计 §8/§9：
- 表级过滤器：软删除/租户/数据权限——定义在语义层（L2），执行在横切（查询层注入）
- 会话变量：运行时占位符（:current_tenant/:current_user/:current_date...）——
  LLM 引用名字，执行层替换真实值，LLM 永不接触真实值

与守卫边（T3/T4）的区别：过滤器的谓词**不依赖对方表**（表一出现就要带，
含单表查询）；守卫边的谓词**依赖对方表**（仅遍历该边时带）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger("kb.filters")

# 会话变量池：LLM 可见的名字（值在执行层替换）
SESSION_VARS: dict[str, dict] = {
    ":current_tenant": {"type": "int",  "desc": "当前租户ID"},
    ":current_user":   {"type": "int",  "desc": "当前用户ID"},
    ":current_date":   {"type": "date", "desc": "今天（本地时区）"},
    ":current_org":    {"type": "int",  "desc": "当前组织ID"},
}

# S2-1：启发式列名候选（行业惯例宽信号，仅作 AI 预标记提示——最终裁决由 LLM 语义确认）
# 软删除列名候选（子串命中即提示）
_SOFT_DELETE_COLS = ("is_deleted", "deleted", "valid", "removed", "deleted_at",
                     "is_valid", "archived", "inactive", "is_archived", "delete_flag", "deleted_flag")
# 租户/组织隔离列名候选（子串命中即提示）
_TENANT_COLS = ("tenant", "org", "company", "shop", "dept", "unit", "branch", "store")
# 反信号：含这些子串的列名不算租户/软删（度量/时间/外键引用）
_TENANT_NEG = ("_at", "_time", "date", "amount", "price", "qty", "count", "num", "_id_ref", "ref_id")


def session_vars_prompt(state: Any = None, conn_id: str | None = None) -> str:
    """注入 context 的会话变量清单文本（LLM 知道有哪些可用，不知道真实值）。

    S2-3：内置变量（行业惯例）+ 连接自定义变量（连接配置 session_vars，
    结构 {name: {value, type, description}}）合并注入。
    """
    lines = ["可用会话变量（写 SQL 时可直接引用这些占位符，执行时系统替换真实值）："]
    for name, meta in SESSION_VARS.items():
        lines.append(f"- {name}：{meta['desc']}（{meta['type']}）")
    if state is not None and conn_id:
        try:
            sv = getattr(state.connections.get(conn_id), "session_vars", None) or {}
            for name, meta in sv.items():
                if name in SESSION_VARS:
                    continue
                if isinstance(meta, dict):
                    desc = meta.get("description") or f"自定义变量 {name}"
                    vtype = meta.get("type") or "str"
                else:
                    desc, vtype = f"自定义变量 {name}", "str"
                lines.append(f"- :{name}：{desc}（{vtype}）")
        except Exception:
            pass
    return "\n".join(lines)


def resolve_session_var(name: str, ctx: dict) -> str:
    """执行时替换：ctx 提供 {current_tenant: 7, current_user: 3, ...}；时间变量取系统时钟。

    S2-3：白名单放宽——内置变量（SESSION_VARS）或连接配置里有值（ctx）即可替换；
    其余 → 抛 KeyError（调用方捕获并告警，不注入错误值）。
    """
    if name not in SESSION_VARS and name.lstrip(":") not in ctx:
        raise KeyError(f"未知会话变量: {name}")
    key = name.lstrip(":")
    if key == "current_date":
        return datetime.now().strftime("%Y-%m-%d")
    if key not in ctx:
        raise KeyError(f"会话变量 {name} 未配置（ctx 缺 {key}）")
    return str(ctx[key])


@dataclass
class TableFilter:
    table: str
    predicate: str            # "is_deleted = 0" 或 "tenant_id = :current_tenant"
    scope: str = "table"      # connection_default | table | exempt
    status: str = "draft"     # draft | confirmed

    def to_dict(self) -> dict[str, Any]:
        return {"table": self.table, "predicate": self.predicate,
                "scope": self.scope, "status": self.status}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TableFilter":
        return cls(table=d.get("table", ""), predicate=d.get("predicate", ""),
                   scope=d.get("scope", "table"), status=d.get("status", "draft"))


class FilterStore:
    """表级过滤器存储：内存态 + 快照持久化（经门面 _load_conn/_save_conn）。"""

    def __init__(self) -> None:
        self._filters: dict[str, dict[str, TableFilter]] = {}  # conn -> table -> filter

    def add(self, conn_id: str, table: str, predicate: str,
            scope: str = "table", status: str = "draft") -> TableFilter:
        """新增/覆盖一条表级过滤器（人工确认页/API 写入）。"""
        f = TableFilter(table=table, predicate=predicate, scope=scope, status=status)
        self._filters.setdefault(conn_id, {})[table] = f
        logger.info("[filters] conn=%s add table=%s scope=%s status=%s", conn_id, table, scope, status)
        return f

    # ---- 结构检测 → AI 预标记候选（S2-1：宽信号启发式，LLM 语义裁决为权威） ----
    def detect_candidates(self, schema: dict) -> list[TableFilter]:
        candidates: list[TableFilter] = []
        cols_by_table: dict[str, list[dict]] = {}
        for c in schema.get("columns", []):
            cols_by_table.setdefault(c["table"], []).append(c)
        for tname, cols in cols_by_table.items():
            for c in cols:
                cname = c["name"].lower()
                if cname in _SOFT_DELETE_COLS or any(
                        s in cname for s in _SOFT_DELETE_COLS if len(s) >= 8):
                    pred = f"{c['name']} IS NULL" if "deleted_at" == cname else f"{c['name']} = 0"
                    candidates.append(TableFilter(table=tname, predicate=pred))
                    break  # 一表最多一个软删除候选
            for c in cols:
                cname = c["name"].lower()
                if any(s in cname for s in _TENANT_COLS) and not any(
                        s in cname for s in _TENANT_NEG):
                    candidates.append(TableFilter(
                        table=tname, predicate=f"{c['name']} = :current_tenant",
                        scope="connection_default"))
                    break  # 一表最多一个租户候选
        logger.info("[filters] detect 候选 %d 个", len(candidates))
        return candidates

    # ---- 查询：合并表级 + 连接级默认，exempt 排除 ----
    def get_filters(self, conn_id: str, tables: set[str]) -> dict[str, list[str]]:
        confirmed = {t: f for t, f in self._filters.get(conn_id, {}).items()
                     if f.status == "confirmed"}
        out: dict[str, list[str]] = {}
        for t in tables:
            f = confirmed.get(t)
            if f is None:
                continue
            if f.scope == "exempt":
                continue  # 豁免：不注入
            out.setdefault(t, []).append(f.predicate)
        return out

    def confirm(self, conn_id: str, table: str) -> bool:
        f = self._filters.get(conn_id, {}).get(table)
        if f is None:
            return False
        f.status = "confirmed"
        logger.info("[filters] conn=%s confirm table=%s", conn_id, table)
        return True

    def reject(self, conn_id: str, table: str) -> bool:
        removed = self._filters.get(conn_id, {}).pop(table, None) is not None
        if removed:
            logger.info("[filters] conn=%s reject table=%s", conn_id, table)
        return removed

    def load(self, conn_id: str, items: list[dict]) -> None:
        self._filters[conn_id] = {d["table"]: TableFilter.from_dict(d) for d in items}

    def dump(self, conn_id: str) -> list[dict]:
        return [f.to_dict() for f in self._filters.get(conn_id, {}).values()]

    def dump_objects(self, conn_id: str) -> list[TableFilter]:
        """返回 TableFilter 对象列表（API 展示用，含 table/predicate/scope/status）。"""
        return list(self._filters.get(conn_id, {}).values())


# ---------------------------------------------------------------- 执行链（R6/T8）

def session_var_ctx(state: Any, conn_id: str) -> dict:
    """会话变量真实值：连接级默认（ConnectionConfig.session_vars）。

    S2-3：支持两种结构——旧值格式 {"current_tenant": 7} 与
    元信息格式 {"current_tenant": {"value": 7, "type": "int", "description": "..."}}；
    统一展开为 {name: 值}。current_date 由系统时钟提供，无需配置。
    """
    try:
        cfg = state.connections.get(conn_id)
        sv = dict(getattr(cfg, "session_vars", None) or {})
        out: dict[str, Any] = {}
        for name, meta in sv.items():
            if isinstance(meta, dict):
                out[name] = meta.get("value", "")
            else:
                out[name] = meta
        return out
    except Exception:
        return {}


def resolve_session_vars_in_sql(sql: str, ctx: dict) -> str:
    """替换 SQL 内的会话变量占位符（LLM 写的 :current_tenant 等），AST 级替换。

    - sqlglot 解析后只替换 Placeholder 节点且键在 SESSION_VARS 中——
      字符串字面量（'a:current_tenant'）不是 Placeholder，绝不误替换
    - 未配置的变量 → warning + 保留占位符（执行层报错可见，不注入错误值）
    - 解析失败 → 原样返回（宁缺勿错）
    """
    try:
        import sqlglot
        expr = sqlglot.parse_one(sql)
    except Exception:
        return sql

    def _rep(node: Any) -> Any:
        if isinstance(node, sqlglot.exp.Placeholder):
            key = f":{node.name or ''}"
            # S2-3：内置变量或连接配置有值（ctx）均可替换
            if key in SESSION_VARS or (node.name or "").lstrip(":") in ctx:
                try:
                    return sqlglot.exp.Literal.string(resolve_session_var(key, ctx))
                except KeyError as e:
                    logger.warning("[filters] %s，占位符保留原样", e)
        return node

    try:
        return expr.transform(_rep).sql()
    except Exception as e:  # pragma: no cover
        logger.warning("[filters] 占位符替换失败，返回原 SQL：%s", e)
        return sql


def prepare_query_sql(state: Any, conn_id: str, sql: str) -> str:
    """查询执行前加工（S1：执行层不再改写 SQL，只做会话变量填充）。

    - 过滤器（租户/软删）已从执行层移除：它们是知识库语义属性，经 context 提示
      LLM 参考织入；执行层不再强制注入（原则：引擎只做执行/拦截/审计，不深入改写）
    - 仅剩会话变量占位符替换（:current_tenant → 连接配置真实值，填充参数语义）
    - 任何异常 → 返回原 SQL（加工失败不阻塞查询链路）
    """
    try:
        ctx = session_var_ctx(state, conn_id)
        return resolve_session_vars_in_sql(sql, ctx)
    except Exception as e:
        logger.warning("[filters] conn=%s 查询加工失败，返回原 SQL：%s", conn_id, e)
        return sql
