"""L1 结构层：GraphEdge 边模型（T3）。

对齐设计 §3.2/§3.4：
- cols 列对列表（复合键：join 需多列 AND 连接）
- guard 守卫谓词（多态关联：X.type = 1）
- confidence/provenance（推断边低置信度、带来源）
- 允许自环（source == target，防环在 BFS）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# 边类型（relation/kind）
RELATION_FK = "fk"
RELATION_NAMING = "naming"
RELATION_VALUE_OVERLAP = "value_overlap"
RELATION_QUERY_LOG = "query_log"
RELATION_USER = "user"
RELATION_SAME_DIMENSION = "same_dimension"

# 来源（provenance）
PROV_DECLARED_FK = "declared_fk"
PROV_NAMING_INFERENCE = "naming_inference"
PROV_VALUE_OVERLAP = "value_overlap"
PROV_QUERY_LOG = "query_log"
PROV_HUMAN = "human"


@dataclass
class GraphEdge:
    """图边：列对列表 + 守卫谓词 + 置信度/来源。"""

    source_table: str
    target_table: str
    cols: list[tuple[str, str]] = field(default_factory=list)  # [("user_id","id"),("line_no","line_no")]
    cardinality: str = "n:1"             # "1:1" | "1:N" | "N:M"
    relation: str = RELATION_FK          # fk|naming|value_overlap|query_log|user|same_dimension
    confidence: float = 1.0
    provenance: str = PROV_DECLARED_FK
    guard: str | None = None             # "X.type = 1"
    weight: float = 1.0
    reason: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.cols:
            raise ValueError("edge requires at least one column pair")

    # ---- 序列化：与既有 dict 格式兼容（from/from_col/to/to_col/kind/...） ----
    def to_dict(self) -> dict[str, Any]:
        """→ 旧 dict 格式（API/前端/存储兼容）；from_col/to_col 取第一对列。"""
        first = self.cols[0]
        return {
            "from": self.source_table,
            "from_col": first[0],
            "to": self.target_table,
            "to_col": first[1],
            "kind": self.relation,
            "weight": self.weight,
            "shared": None,
            "cardinality": self.cardinality,
            "reason": self.reason,
            "guard": self.guard,
            "confidence": self.confidence,
            "provenance": self.provenance,
            "cols": [list(pair) for pair in self.cols],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GraphEdge":
        cols = d.get("cols")
        if isinstance(cols, str):
            # 防御：脏数据（存储未解析的 JSON 字符串）兜底解析
            try:
                import json
                cols = json.loads(cols)
            except (TypeError, ValueError):
                cols = None
        if cols:
            pairs = [tuple(p) for p in cols]
        else:
            pairs = [(d.get("from_col") or "", d.get("to_col") or "")]
        return cls(
            source_table=d.get("from", d.get("source_table", "")),
            target_table=d.get("to", d.get("target_table", "")),
            cols=pairs,
            cardinality=d.get("cardinality", "n:1"),
            relation=d.get("kind", d.get("relation", RELATION_FK)),
            confidence=float(d.get("confidence") or 1.0),
            provenance=d.get("provenance", PROV_DECLARED_FK),
            guard=d.get("guard"),
            weight=float(d.get("weight") or 1.0),
            reason=d.get("reason", ""),
            metadata=dict(d.get("metadata") or {}),
        )

    # ---- join 条件序列化 ----
    def join_condition(self, quote: Callable[[str], str] = lambda x: x) -> str:
        """序列化 join 条件：
        - 单列：  "orders.user_id = users.id"
        - 复合：  "orders.order_id = users.id AND orders.line_no = users.line_no"
        - 守卫：  追加 " AND X.type = 1"
        """
        s = quote(self.source_table)
        t = quote(self.target_table)
        parts = [f"{s}.{quote(sc)} = {t}.{quote(tc)}" for sc, tc in self.cols]
        cond = " AND ".join(parts)
        if self.guard:
            cond = f"{cond} AND {self.guard}"
        return cond

    # ---- 身份键（去重/墓碑匹配用） ----
    def key(self) -> tuple[str, str, str, str, str]:
        """(source_table, target_table, cols, relation, guard) 归一化键。"""
        cols_key = "|".join(f"{a}~{b}" for a, b in self.cols)
        return (self.source_table, self.target_table, cols_key, self.relation, self.guard or "")
