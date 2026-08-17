"""安全闸门数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Tier(str, Enum):
    READ = "read"
    DML = "dml"
    DDL = "ddl"
    UNKNOWN = "unknown"


class Verdict(str, Enum):
    ALLOW = "allow"      # 读：直接执行
    REVIEW = "review"    # 写/手动 DDL：需确认，DML 附影响行数预览
    BLOCK = "block"      # 拒绝：不执行


class Origin(str, Enum):
    AI = "ai"
    MANUAL = "manual"


@dataclass
class RuleResult:
    rule: str
    verdict: Verdict
    reason: str
    tier: Tier | None = None


@dataclass
class Assessment:
    verdict: Verdict
    tier: Tier
    reasons: list[str] = field(default_factory=list)
    rules: list[RuleResult] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    has_where: bool | None = None
    has_limit: bool | None = None
    parse_error: str | None = None
    is_multi: bool = False

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "tier": self.tier.value,
            "reasons": self.reasons,
            "rules": [
                {"rule": r.rule, "verdict": r.verdict.value, "reason": r.reason}
                for r in self.rules
            ],
            "tables": self.tables,
            "has_where": self.has_where,
            "has_limit": self.has_limit,
            "parse_error": self.parse_error,
            "is_multi": self.is_multi,
        }
