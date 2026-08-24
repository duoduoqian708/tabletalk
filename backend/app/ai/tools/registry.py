"""原子工具注册表：每个工具 = 名称 + schema + 执行函数，execute_tool 按名分发。

各工具模块（sql.py / schema_tools.py）import 后调用 register_tool 注册，运行时由
execute_tool 按工具名派发。这是 skill 框架的底层积木——任一技能引用的是这里注册的原子工具。
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

# 当前会话（T3.3 load_result 按 session 隔离工件；跨 session 拒绝）。
# loop 在每次 execute_tool 前设置，工具内读取；不进 handler 参数，避免扩大全量签名。
_ACTIVE_SESSION: contextvars.ContextVar[str | None] = contextvars.ContextVar("active_session", default=None)


def set_active_session(session_id: str | None):
    return _ACTIVE_SESSION.set(session_id)


def reset_active_session(token):
    _ACTIVE_SESSION.reset(token)


def get_active_session() -> str | None:
    return _ACTIVE_SESSION.get()

if TYPE_CHECKING:
    from app.state import AppState


@dataclass
class ToolOutcome:
    result: dict[str, Any]
    card: dict[str, Any] | None = None
    think: str | None = None


ToolHandler = Callable[..., Awaitable[ToolOutcome]]

TRUST_LEVELS = ("readonly", "mutating", "destructive")
CONFIRM_MODES = ("none", "card", "admin")

TOOL_SCHEMAS: list[dict] = []
TOOL_META: dict[str, dict] = {}
_TOOL_HANDLERS: dict[str, ToolHandler] = {}


def _tool(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


def register_tool(
    name: str,
    description: str,
    props: dict,
    required: list[str],
    handler: ToolHandler,
    *,
    trust: str | None = None,
    confirm: str = "none",
    audit_source: str | None = None,
) -> None:
    # 铁律 3：能力"不能做"的最强保证是 tool 不存在；trust 元数据是注册强制项
    if trust not in TRUST_LEVELS:
        raise ValueError(f"tool '{name}' 缺少合法 trust（{TRUST_LEVELS}），拒绝注册")
    if confirm not in CONFIRM_MODES:
        raise ValueError(f"tool '{name}' confirm 必须为 {CONFIRM_MODES}")
    TOOL_SCHEMAS.append(_tool(name, description, props, required))
    _TOOL_HANDLERS[name] = handler
    TOOL_META[name] = {"trust": trust, "confirm": confirm, "audit_source": audit_source}


def validate_registry() -> None:
    missing = [n for n in _TOOL_HANDLERS if n not in TOOL_META]
    if missing:
        raise ValueError(f"工具缺 trust 元数据，启动自检失败: {missing}")


def tool_schemas(readonly: bool = False) -> list[dict]:
    if not readonly:
        return list(TOOL_SCHEMAS)
    allow = {"get_schema", "run_query", "query_audit", "ai_review"}
    return [t for t in TOOL_SCHEMAS if t["function"]["name"] in allow]


async def execute_tool(
    state: "AppState",
    name: str,
    args: dict[str, Any],
    conn_id: str,
    include_data: bool = False,
) -> ToolOutcome:
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return ToolOutcome(result={"ok": False, "error": f"未知工具: {name}"})
    return await handler(state=state, args=args, conn_id=conn_id, include_data=include_data)


def _get_ctx(state: "AppState", conn_id: str):
    from app.safety import gate as safety_gate

    cfg = state.connections.get(conn_id)
    return cfg, safety_gate.sqlglot_dialect_for(cfg.dialect)


def _sub(sql: str, sqlglot_dialect: str) -> str:
    from app.safety import parser as safety_parser

    infos = safety_parser.parse_sql(sql, sqlglot_dialect)
    if not infos:
        return ""
    i = infos[0]
    extra = f" · {len(i.tables)} tables" if i.tables else ""
    return f"{i.stmt_type.upper()}{extra}"
