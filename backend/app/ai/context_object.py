"""E3 Context 层间 I/O 契约（设计 §18）。

各层只认这一个对象，不互相直接调内部。跨任务共享前序结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ai.plan import TaskPlan, TaskSpec


@dataclass
class Context:
    conn_id: str
    session_id: str | None = None
    plan: TaskPlan | None = None
    current_task: TaskSpec | None = None
    current_skill: str | None = None
    selected_tables: list[str] = field(default_factory=list)  # 图/知识库选出的表集合（跨层共享）
    sql_draft: str | None = None
    task_results: dict[str, Any] = field(default_factory=dict)  # task.id → result 块
    include_data: bool = False
    session_vars: dict[str, str] = field(default_factory=dict)  # 会话变量（执行层替换）

    def get_result(self, task_id: str) -> Any | None:
        return self.task_results.get(task_id)

    def set_result(self, task_id: str, result: Any) -> None:
        self.task_results[task_id] = result
