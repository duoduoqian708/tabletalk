"""知识库 v3 存储模型 v2：按表组织（TableKnowledge/ColumnInfo）。

数据源接入 → build()（结构抽取 + 采样 + 逐表 AI 注释[含取值对照/示例] + 图谱 +
向量索引 + 持久化）→ 审查工作台逐列 ✓/✕ 确认 → retrieve()/route_tables()
喂给 AI 上下文。

隐私：采样只存本地；发送给模型的注释 prompt 是否含样本值由 `kb_ai_annotation_samples`
门控（未授权 → 无样本 → values/example 为空）；嵌入需通过 OpenAI 兼容 API 接入。
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field, fields
from typing import Any

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
    status: str = "none"   # 当前内容状态：none | confirmed（comment+values 整体）

    # ---- 当前生效版与本轮提案并行 ----
    # proposed_* 是本轮 AI 提案（重建产生，与当前值并存）：
    # 确认 = 提案提升为当前（proposal -> comment, status=confirmed）；
    # 保持当前/拒绝 = 清除提案，当前值不动。审查页对比按钮读两组字段。
    proposed_comment: str = ""   # 本轮提案（空 = AI 无提案）
    proposed_values: str = ""
    proposed_example: str = ""
    core: bool = False   # AI 关键列提名（合成器向量化选材信号，非用户内容）

    @property
    def has_proposal(self) -> bool:
        return bool(self.proposed_comment or self.proposed_values or self.proposed_example)

    def apply_proposal(self) -> bool:
        """提案提升为当前（空提案项保留当前值）。返回是否有变化。"""
        changed = False
        if self.proposed_comment:
            self.comment = self.proposed_comment
            changed = True
        if self.proposed_values:
            self.values = self.proposed_values
            changed = True
        if self.proposed_example:
            self.example = self.proposed_example
            changed = True
        self.proposed_comment = ""
        self.proposed_values = ""
        self.proposed_example = ""
        return changed

    def clear_proposal(self) -> bool:
        """保持当前：清除提案。返回是否有变化。"""
        if self.has_proposal:
            self.proposed_comment = ""
            self.proposed_values = ""
            self.proposed_example = ""
            return True
        return False

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
    vector_override: str = ""  # 人工覆盖的向量化片段文本；空 = 用 AI 画像
    vector_profile: str = ""   # AI 表画像（审核确认后的向量化文本；无代码拼接）
    proposed_comment: str = ""  # 本轮 AI 提案（表级，与当前 comment 并存）
    proposed_profile: str = ""  # 本轮 AI 画像提案（confirm 时与 proposed_comment 一起提升）

    @property
    def has_proposal(self) -> bool:
        return bool(self.proposed_comment or self.proposed_profile)

    def apply_proposal(self) -> bool:
        changed = False
        if self.proposed_comment:
            self.comment = self.proposed_comment
            self.proposed_comment = ""
            changed = True
        if self.proposed_profile:
            self.vector_profile = self.proposed_profile
            self.proposed_profile = ""
            changed = True
        return changed

    def clear_proposal(self) -> bool:
        if self.has_proposal:
            self.proposed_comment = ""
            self.proposed_profile = ""
            return True
        return False

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
            "vector_profile": self.vector_profile,
            "proposed_comment": self.proposed_comment,
            "proposed_profile": self.proposed_profile,
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
            vector_profile=d.get("vector_profile", ""),
            proposed_comment=d.get("proposed_comment", ""),
            proposed_profile=d.get("proposed_profile", ""),
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

