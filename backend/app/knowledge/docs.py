"""知识库文档模型。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class KnowledgeDoc:
    id: str
    conn_id: str
    kind: str                 # table | column | fk | note | glossary
    title: str
    body: str
    table: str | None = None
    column: str | None = None
    tags: list[str] = field(default_factory=list)
    source: str = "auto"      # auto | user | ai_draft
    status: str = "confirmed" # confirmed（权威）| draft（AI 草案，待人工确认）
    updated_at: str = ""
    archived: bool = False    # 表被删除 → 结构文档归档（保留可回溯，检索/图谱过滤）

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "conn_id": self.conn_id,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "table": self.table,
            "column": self.column,
            "tags": self.tags,
            "source": self.source,
            "status": self.status,
            "updated_at": self.updated_at,
            "archived": self.archived,
        }
