"""方言适配器抽象。新增数据库 = 实现本接口 + 注册进 registry，核心逻辑零改动。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RawResult:
    columns: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    rowcount: int = 0          # DML 受影响行数
    is_dml: bool = False


@dataclass
class DialectConfig:
    """统一连接配置视图，各适配器自行解释。"""
    host: str = ""
    port: int | None = None
    user: str = ""
    password: str = ""
    database: str = ""
    file: str = ""             # SQLite 路径
    ssl: bool = False
    read_only: bool = False
    timeout: int = 10


@dataclass
class TableRef:
    name: str
    kind: str = "table"        # table | view
    comment: str = ""


@dataclass
class ColumnRef:
    table: str
    name: str
    data_type: str
    nullable: bool = True
    is_pk: bool = False
    is_fk: bool = False
    default: str | None = None
    comment: str = ""


@dataclass
class FKRef:
    table: str
    column: str
    ref_table: str
    ref_column: str


class DialectAdapter(ABC):
    name: str = "abstract"
    sqlglot_name: str = ""

    @abstractmethod
    async def connect(self, cfg: DialectConfig) -> Any:
        ...

    @abstractmethod
    async def close(self, conn: Any) -> None:
        ...

    @abstractmethod
    async def is_healthy(self, conn: Any) -> bool:
        ...

    @abstractmethod
    async def execute(self, conn: Any, sql: str) -> RawResult:
        """执行任意单条 SQL。SELECT 返回列/行；DML 返回 rowcount。"""

    @abstractmethod
    async def list_tables(self, conn: Any) -> list[TableRef]:
        ...

    @abstractmethod
    async def list_columns(self, conn: Any, table: str) -> list[ColumnRef]:
        ...

    @abstractmethod
    async def list_foreign_keys(self, conn: Any) -> list[FKRef]:
        ...

    @abstractmethod
    async def count_rows(self, conn: Any, table: str) -> int:
        """返回表的总行数（用于前端图谱节点大小）。"""

    @abstractmethod
    def quote_ident(self, name: str) -> str:
        ...

    @abstractmethod
    def quote_literal(self, value: Any) -> str:
        ...

    async def explain(self, conn: Any, sql: str) -> dict[str, Any]:
        """估算查询成本（行数、上界）。默认不实现，返回空，由调用方降级为不做成本检查。"""
        return {"estimated_rows": None, "is_scan": False, "detail": "not implemented"}

    async def set_read_only(self, conn: Any, flag: bool) -> None:
        """连通测试**通过后**才把"是否只读"应用到数据库会话（标记/强化）。

        设计约定：connect() 一律裸连接，不做任何会话设置——read_only 是应用层标记
        （闸门用它拦截写操作），数据库会话级只读仅在测试通过后按需补设。
        默认 no-op；PG/MySQL 各自实现。
        """
