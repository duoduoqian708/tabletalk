"""tabletalk-gate public API — re-export from safety engine."""
from .gate import assess_sql, preview_count_query, suggest_safe, sqlglot_dialect_for
from .models import Assessment, Origin, RuleResult, Tier, Verdict

def assess(sql: str, dialect: str = "sqlite", origin: Origin | str = Origin.MANUAL, **kwargs):
    # 兼容 dialect / sqlglot_dialect 两种参数名
    d = kwargs.get("sqlglot_dialect", dialect)
    # origin 兼容字符串
    if isinstance(origin, str):
        try:
            origin = Origin(origin)
        except Exception:
            origin = Origin.MANUAL
    return assess_sql(sql, d, origin)

__all__ = ["assess", "assess_sql", "preview_count_query", "suggest_safe", "sqlglot_dialect_for", "Assessment", "Origin", "RuleResult", "Tier", "Verdict"]
