"""L2 语义层：概念字典（T7）。

对齐设计 §5 + §8.2：
- Concept：规范枚举 + 成员列值映射；kind=dimension（维度）/ constant（静态业务常量）
- 一列一概念（冲突校验）；归属必须人工确认（同名不同义防误伤）
- 防漂移：成员列采样新值 → 提示人工确认，不自动写
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("kb.concepts")


@dataclass
class Concept:
    name: str
    canonical_enum: list[dict] = field(default_factory=list)  # [{"code","label"}]
    members: list[dict] = field(default_factory=list)         # [{"table","column","mapping"}]
    status: str = "draft"            # draft | confirmed
    kind: str = "dimension"          # dimension | constant
    updated_at: str = ""
    source: str = ""                 # human | sampling

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "canonical_enum": self.canonical_enum,
            "members": self.members,
            "status": self.status,
            "kind": self.kind,
            "updated_at": self.updated_at,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Concept":
        return cls(
            name=d.get("name", ""),
            canonical_enum=list(d.get("canonical_enum") or []),
            members=list(d.get("members") or []),
            status=d.get("status", "draft"),
            kind=d.get("kind", "dimension"),
            updated_at=d.get("updated_at", ""),
            source=d.get("source", ""),
        )


class ConceptStore:
    """概念字典存储：内存态 + 快照持久化（经门面 _load_conn/_save_conn）。"""

    def __init__(self) -> None:
        self._concepts: dict[str, list[Concept]] = {}  # conn -> [Concept]

    # ---- 基础 CRUD ----
    def upsert(self, conn_id: str, c: Concept, schema: dict | None = None) -> bool:
        """新增/覆盖概念。校验：一列一概念冲突 + 成员列必须存在于 schema（T7 §5#1/#5）。"""
        if not c.name:
            return False
        # 成员列 schema 校验：引用的 (table, column) 不存在 → 拒绝（schema 提供时）
        if schema is not None:
            cols = {(col["table"], col["name"]) for col in schema.get("columns", [])}
            for m in c.members:
                key = (m.get("table", ""), m.get("column", ""))
                if not key[0] or not key[1] or key not in cols:
                    logger.warning("[concepts] conn=%s 概念 %s 成员列不存在：%s.%s",
                                   conn_id, c.name, m.get("table"), m.get("column"))
                    return False
        others = self._concepts.get(conn_id, [])
        # 冲突校验：成员列不与其他概念重复
        claimed: set[tuple[str, str]] = set()
        for other in others:
            if other.name == c.name:
                continue
            for m in other.members:
                claimed.add((m.get("table", ""), m.get("column", "")))
        for m in c.members:
            key = (m.get("table", ""), m.get("column", ""))
            if key in claimed:
                logger.warning("[concepts] conn=%s 概念 %s 成员列冲突：%s.%s 已属于其他概念",
                               conn_id, c.name, m.get("table"), m.get("column"))
                return False
        # 同名覆盖：保留 confirmed 状态不降级
        existing = next((x for x in others if x.name == c.name), None)
        if existing and existing.status == "confirmed" and c.status == "draft":
            c.status = "confirmed"
        self._concepts[conn_id] = [x for x in others if x.name != c.name] + [c]
        logger.info("[concepts] conn=%s upsert concept=%s kind=%s status=%s",
                    conn_id, c.name, c.kind, c.status)
        return True

    def list(self, conn_id: str) -> list[Concept]:
        return list(self._concepts.get(conn_id, []))

    def get(self, conn_id: str, name: str) -> Concept | None:
        return next((c for c in self._concepts.get(conn_id, []) if c.name == name), None)

    def get_for_column(self, conn_id: str, table: str, column: str) -> Concept | None:
        """按 (table, column) 命中成员列 → 所属概念（值落地检索用）。"""
        for c in self._concepts.get(conn_id, []):
            if any(m.get("table") == table and m.get("column") == column for m in c.members):
                return c
        return None

    def confirm(self, conn_id: str, name: str) -> bool:
        c = self.get(conn_id, name)
        if c is None:
            return False
        c.status = "confirmed"
        logger.info("[concepts] conn=%s confirm concept=%s", conn_id, name)
        return True

    def reject(self, conn_id: str, name: str) -> bool:
        before = len(self._concepts.get(conn_id, []))
        self._concepts[conn_id] = [c for c in self._concepts.get(conn_id, []) if c.name != name]
        removed = len(self._concepts.get(conn_id, [])) < before
        if removed:
            logger.info("[concepts] conn=%s reject concept=%s", conn_id, name)
        return removed

    def load(self, conn_id: str, items: list[dict]) -> None:
        self._concepts[conn_id] = [Concept.from_dict(d) for d in items]

    def dump(self, conn_id: str) -> list[dict]:
        return [c.to_dict() for c in self._concepts.get(conn_id, [])]

    # ---- 解析：平铺 values → 概念条目候选 ----
    @staticmethod
    def parse_values_to_candidates(values_text: str) -> list[dict]:
        """解析 "P=待付款;S=已发货" → [{code:"P",label:"待付款"}]。

        支持分隔符：；;、，, 换行；条目格式 `code=label` 或 `code：label`。
        格式不标准 → 返回 []（宁可弃不自动写）。
        """
        if not values_text or not values_text.strip():
            return []
        out: list[dict] = []
        for chunk in values_text.replace("\n", ";").replace("；", ";").replace("，", ",").split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            for sep in ("=", "：", ":"):
                if sep in chunk:
                    code, label = chunk.split(sep, 1)
                    code, label = code.strip(), label.strip()
                    if code and label:
                        out.append({"code": code, "label": label})
                    break
        return out

    # ---- 防漂移：成员列采样新值提示 ----
    def detect_drift(self, conn_id: str, table: str, column: str, sampled_values: list) -> list[str]:
        """成员列采样值与 canonical_enum 比对 → 返回不在枚举中的新值（提示人工确认，不自动写）。"""
        c = self.get_for_column(conn_id, table, column)
        if c is None:
            return []
        known = {str(m.get("code")) for m in c.canonical_enum}
        new_vals = sorted({str(v) for v in sampled_values if v is not None and str(v) not in known})
        if new_vals:
            logger.info("[concepts] conn=%s 防漂移：concept=%s 新值 %s（待确认）",
                        conn_id, c.name, new_vals)
        return new_vals
