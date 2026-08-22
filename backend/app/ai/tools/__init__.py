"""工具包：原子工具注册表 + 各工具模块。import 本包即触发各工具注册。

对外保持与旧 `app.ai.tools` 兼容的导出（TOOL_SCHEMAS / execute_tool / ToolOutcome），
因此既有调用方 `from app.ai.tools import ...` 无需改动。
"""
from __future__ import annotations

from app.ai.tools.registry import (
    ToolOutcome,
    _sub,
    execute_tool,
    get_active_session,
    register_tool,
    reset_active_session,
    set_active_session,
    tool_schemas,
)
from app.ai.tools import schema_tools, sql  # noqa: F401 （import 触发 register）
from app.ai.tools import load_result  # noqa: F401 （WS3 T3.3）

# 注册内置工具
schema_tools.register()
sql.register()
load_result.register()

# 兼容旧命名（原 tools.py 的模块级常量）
TOOL_SCHEMAS = tool_schemas(readonly=False)
TOOL_SCHEMAS_READONLY = tool_schemas(readonly=True)
