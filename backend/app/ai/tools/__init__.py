"""工具包：原子工具注册表 + 各工具模块。import 本包即触发各工具注册。

公共注册表在 app.ai.tools.registry；对外保持 TOOLS_SCHEMAS / execute_tool / ToolOutcome 兼容导出。
"""
from __future__ import annotations

from app.ai.tools.registry import (
    TOOL_META,
    ToolOutcome,
    execute_tool,
    get_active_session,
    register_tool,
    reset_active_session,
    set_active_session,
    tool_schemas,
    validate_registry,
)

# 注册内置工具（import 触发 register()）
from app.ai.tools import schema_tools, sql, query_audit, ai_review  # noqa: F401
from app.ai.tools import kb_read, kb_write, graph_read, graph_write  # noqa: F401
from app.ai.tools import manage_task, suggest_followup  # noqa: F401

schema_tools.register()
sql.register()
query_audit.register()
ai_review.register()
kb_read.register()
kb_write.register()
graph_read.register()
graph_write.register()
manage_task.register()
suggest_followup.register()

# 兼容旧命名
TOOL_SCHEMAS = tool_schemas(readonly=False)
TOOL_SCHEMAS_READONLY = tool_schemas(readonly=True)
