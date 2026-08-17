"""方言包：导入即完成全部注册（插件式）。新增数据库 = 新建适配器 + 在此导入。"""
from __future__ import annotations

from app.core.dialects.base import (
    ColumnRef,
    DialectAdapter,
    DialectConfig,
    FKRef,
    RawResult,
    TableRef,
)
from app.core.dialects.registry import get_dialect, register_dialect, registry

# 导入即注册——任何代码 import app.core.dialects 后注册表即可用
from app.core.dialects import mysql, postgres, sqlite  # noqa: F401

__all__ = [
    "DialectAdapter",
    "DialectConfig",
    "RawResult",
    "TableRef",
    "ColumnRef",
    "FKRef",
    "registry",
    "get_dialect",
    "register_dialect",
]
