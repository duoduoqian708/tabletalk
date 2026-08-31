"""知识库 v3 存储模型 v2：按表组织（TableKnowledge/ColumnInfo）。

数据源接入 → build()（结构抽取 + 采样 + 逐表 AI 注释[含取值对照/示例] + 图谱 +
向量索引 + 持久化）→ 审查工作台逐列 ✓/✕ 确认 → retrieve()/route_tables()
喂给 AI 上下文。

隐私：采样只存本地；发送给模型的注释 prompt 是否含样本值由 `kb_ai_annotation_samples`
门控（未授权 → 无样本 → values/example 为空）；嵌入默认离线哈希，真语义嵌入由设置选择。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from app.core.timeutil import utcnow_iso
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.knowledge.docs import KnowledgeDoc
from app.knowledge.embedding import Embedder, HashingEmbedder, cosine, make_embedder
from app.knowledge.vectorstore import NumpyVectorStore, VectorChunk, VectorStore

if TYPE_CHECKING:
    from app.core.settings import SettingsStore

logger = logging.getLogger(__name__)


@dataclass
class ColumnInfo:
    """列级知识（v2）：结构壳（type/pk/fk/db_comment）+ AI/人工知识字段。"""
    name: str
    type: str = ""
    pk: bool = False
    fk: bool = False
    db_comment: str = ""
    comment: str = ""      # 业务含义（AI 生成 → 人工确认）
    values: str = ""       # 取值对照 "P=待付款；S=已发货；R=已退货"
    example: str = ""      # 示例值（首个非空样本，截断 60 字符）
    status: str = "none"   # none | draft | confirmed（comment+values 整体确认）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ColumnInfo":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class TableKnowledge:
    """表级知识（快照 v2 的核心存储单元，一表一块）。"""
    name: str
    db_comment: str = ""
    column_count: int = 0
    comment: str = ""
    status: str = "none"   # none | draft | confirmed（表级注释状态）
    columns: dict[str, ColumnInfo] = field(default_factory=dict)
    ddl: str = ""
    excluded: bool = False
    layout: dict[str, Any] = field(default_factory=dict)  # 2D 图布局坐标（透传存储，渲染在 T4）
    vector_override: str = ""  # 人工覆盖的向量化片段文本；空 = 用构建合成文本

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "db_comment": self.db_comment,
            "column_count": self.column_count,
            "comment": self.comment,
            "status": self.status,
            "columns": {k: v.to_dict() for k, v in self.columns.items()},
            "ddl": self.ddl,
            "excluded": self.excluded,
            "layout": self.layout,
            "vector_override": self.vector_override,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TableKnowledge":
        cols = {k: ColumnInfo.from_dict(v) for k, v in (d.get("columns") or {}).items()}
        return cls(
            name=d.get("name", ""),
            db_comment=d.get("db_comment", ""),
            column_count=int(d.get("column_count", 0) or 0),
            comment=d.get("comment", ""),
            status=d.get("status", "none"),
            columns=cols,
            ddl=d.get("ddl", ""),
            excluded=bool(d.get("excluded")),
            layout=dict(d.get("layout") or {}),
            vector_override=d.get("vector_override", ""),
        )


@dataclass
class TableCard:
    """检索命中的表知识卡（一表一卡，spec §4）：text=可读表描述，payload=结构化信息。"""
    table: str
    text: str
    payload: dict[str, Any]
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "text": self.text,
            "payload": self.payload,
            "score": self.score,
        }

# 向后兼容重导出：T1 拆分后 store.py 只保留数据类，
# KnowledgeBase 新实现在 facade.py（门面，委托 graph/semantic/retrieval/build 子模块）
from app.knowledge.facade import KnowledgeBase  # noqa: E402,F401
