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
