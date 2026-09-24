"""安全闸门数据模型 — A1 拦截可解释：结构化 reasons。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Tier(str, Enum):
    READ = "read"
    DML = "dml"
    DDL = "ddl"
    UNKNOWN = "unknown"


class Verdict(str, Enum):
    ALLOW = "allow"
    REVIEW = "review"
    BLOCK = "block"


class Origin(str, Enum):
    AI = "ai"
    MANUAL = "manual"
    SCHEDULED = "scheduled"  # 定时任务脚本身份（经 SDK→sidecar，受同样闸门约束）


@dataclass
class RuleResult:
    rule: str  # rule_id
    verdict: Verdict
    reason: str  # 中文主文案（兼容旧字段）
    tier: Tier | None = None
    reason_en: str = ""  # 英文文案
    objects: list[str] = field(default_factory=list)  # 涉及对象（表/列/片段）


@dataclass
class Assessment:
    verdict: Verdict
    tier: Tier
    # 结构化原因：[{rule_id, message, message_en, objects}]
    reasons: list[dict] = field(default_factory=list)
    rules: list[RuleResult] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    has_where: bool | None = None
    has_limit: bool | None = None
    parse_error: str | None = None
    is_multi: bool = False

    def to_dict(self) -> dict:
        # 向后兼容：保留字符串 reasons 的旧行为通过 reason_texts 辅助
        return {
            "verdict": self.verdict.value,
            "tier": self.tier.value,
            "reasons": self.reasons,
            "reason_text": "; ".join(r.get("message", "") for r in self.reasons),
            "rules": [
                {
                    "rule": r.rule,
                    "verdict": r.verdict.value,
                    "reason": r.reason,
                    "reason_en": r.reason_en,
                    "objects": r.objects,
                    "tier": r.tier.value if r.tier else None,
                }
                for r in self.rules
            ],
            "tables": self.tables,
            "has_where": self.has_where,
            "has_limit": self.has_limit,
            "parse_error": self.parse_error,
            "is_multi": self.is_multi,
        }
