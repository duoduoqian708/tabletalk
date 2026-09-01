"""E1 引擎任务规划数据模型（设计 §13.1/§13.2）：TaskSpec/TaskPlan + trust 派生。

- action：封闭枚举（含 unknown 兜底）
- modality：有序枚举 answer ⊂ analyze ⊂ report ⊂ automate（可沿尺度升降级）
- trust：由 action 查表派生（query→read, write→write, ddl→ddl），不分类识别
- target：自由抽取（非枚举），只影响 prompt 组装
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 封闭枚举（含 unknown 兜底）
VALID_ACTIONS = frozenset({"query", "write", "ddl", "kb", "schedule", "system", "unknown"})
# 有序枚举：包含关系，可沿尺度升降级
MODALITY_ORDER = ("answer", "analyze", "report", "automate")

# trust 派生表（设计 §13.2#1：trust 不分类，由 action 查表派生）
_TRUST_BY_ACTION = {
    "query": "read",
    "write": "write",
    "ddl": "ddl",
    "kb": "write",
    "schedule": "write",
    "system": "read",
    "unknown": "read",  # 未知组合兜底为只读低危（general 承接）
}


@dataclass
class TaskSpec:
    """一个任务 = 工作单元。id 供 Context.task_results 索引。"""
    action: str
    modality: str = "answer"
    target: dict[str, Any] = field(default_factory=dict)  # {tables?: [...], concept?: ...}
    id: str = ""

    def __post_init__(self) -> None:
        if self.action not in VALID_ACTIONS:
            self.action = "unknown"
        if self.modality not in MODALITY_ORDER:
            self.modality = "answer"

    @property
    def trust(self) -> str:
        """派生值：由 action 查表（不分类识别）。"""
        return _TRUST_BY_ACTION.get(self.action, "read")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "modality": self.modality,
            "target": self.target or {},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskSpec":
        return cls(
            action=str(d.get("action", "unknown")),
            modality=str(d.get("modality", "answer")),
            target=dict(d.get("target") or {}),
            id=str(d.get("id", "")),
        )


@dataclass
class TaskPlan:
    """意图层输出：任务列表（长度 ≥1）+ plan 级辅助字段。"""
    tasks: list[TaskSpec] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)          # 领域标签（路由种子，plan 级共享）
    followup_tables: list[str] = field(default_factory=list)  # 追问轮种子
    skip_retrieval: bool = False                            # 结构问答跳过检索管线
    degraded: bool = False                                  # 走了关键词/mock 降级
    raw_question: str = ""
    clarify: list[str] = field(default_factory=list)        # 2026-09 §13.4：意图不完整/不清晰 → 候选澄清问题（非空则执行前刹停）

    def __post_init__(self) -> None:
        if not self.tasks and not self.clarify:
            self.tasks = [TaskSpec(action="query", modality="answer")]

    @property
    def has_write(self) -> bool:
        return any(t.trust == "write" for t in self.tasks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks": [t.to_dict() for t in self.tasks],
            "tags": self.tags,
            "followup_tables": self.followup_tables,
            "skip_retrieval": self.skip_retrieval,
            "degraded": self.degraded,
            "clarify": self.clarify,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskPlan":
        """P3：序列化往返（任务/计划级辅助字段全量还原）。"""
        return cls(
            tasks=[TaskSpec.from_dict(t) for t in (d.get("tasks") or [])],
            tags=list(d.get("tags") or []),
            followup_tables=list(d.get("followup_tables") or []),
            skip_retrieval=bool(d.get("skip_retrieval", False)),
            degraded=bool(d.get("degraded", False)),
            raw_question=str(d.get("raw_question", "")),
            clarify=list(d.get("clarify") or []),
        )


def trust_for(action: str) -> str:
    """由 action 查表派生 trust（模块级函数，供路由/安全校验用）。"""
    return _TRUST_BY_ACTION.get(action, "read")