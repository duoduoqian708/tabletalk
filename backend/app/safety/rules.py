"""风险规则引擎：StatementInfo → RuleResult。

闸门与 AI 无关：AI 生成和手动执行的 SQL 走同一套规则。
"""
from __future__ import annotations

from app.safety.models import Origin, RuleResult, Tier, Verdict
from app.safety.parser import StatementInfo


def _rules_for(info: StatementInfo, origin: Origin) -> list[RuleResult]:
    out: list[RuleResult] = []

    # R1 解析失败/未知 → 按写处理，永不 ALLOW
    if info.parse_error or info.kind == "unknown":
        out.append(RuleResult(
            rule="parse-failure",
            verdict=Verdict.REVIEW,
            tier=Tier.UNKNOWN,
            reason=f"无法可靠解析该语句（{info.parse_error or '未知类型'}），按写操作对待，需确认。",
        ))
        return out

    # R2 DDL：AI 物理上无 DDL 工具，此路径不应发生 → 硬拦截
    if info.kind == "ddl":
        if origin == Origin.AI:
            out.append(RuleResult(
                rule="ddl-ai",
                verdict=Verdict.BLOCK,
                tier=Tier.DDL,
                reason="AI 没有 DDL 工具，无法执行 DDL。请将脚本发送到编辑器手动运行。",
            ))
        else:
            out.append(RuleResult(
                rule="ddl-manual",
                verdict=Verdict.REVIEW,
                tier=Tier.DDL,
                reason="DDL 属于高危操作，需手动强确认。",
            ))
        return out

    # R3 UPDATE/DELETE 无 WHERE → 拦截
    if info.stmt_type in ("update", "delete") and not info.has_where:
        out.append(RuleResult(
            rule="dml-no-where",
            verdict=Verdict.BLOCK,
            tier=Tier.DML,
            reason="UPDATE/DELETE 缺少 WHERE 条件——这会是全表写，已拦截。请加 WHERE 或先 SELECT 确认目标。",
        ))
        return out

    # R4 其余 DML → 需确认
    if info.kind == "dml":
        out.append(RuleResult(
            rule="dml-confirm",
            verdict=Verdict.REVIEW,
            tier=Tier.DML,
            reason="写操作需确认。已估算影响行数，请核对后执行。",
        ))
        return out

    # R5 事务控制 → 需确认
    if info.kind == "tcl":
        out.append(RuleResult(
            rule="tcl-confirm",
            verdict=Verdict.REVIEW,
            tier=Tier.UNKNOWN,
            reason="事务控制语句需确认。",
        ))
        return out

    # R6 读 → 放行
    if info.kind == "read":
        if info.stmt_type == "select" and not info.has_limit:
            out.append(RuleResult(
                rule="read-no-limit",
                verdict=Verdict.ALLOW,
                tier=Tier.READ,
                reason="只读放行。未加 LIMIT，若表很大建议加 LIMIT 控制返回量。",
            ))
        else:
            out.append(RuleResult(
                rule="read-allow",
                verdict=Verdict.ALLOW,
                tier=Tier.READ,
                reason="只读语句，直接放行。",
            ))
        return out

    # 兜底：不应到达
    out.append(RuleResult(
        rule="unknown-fallback",
        verdict=Verdict.REVIEW,
        tier=Tier.UNKNOWN,
        reason="无法识别的语句，需确认。",
    ))
    return out


def run_rules(infos: list[StatementInfo], origin: Origin) -> list[RuleResult]:
    results: list[RuleResult] = []

    # R7 多语句批处理：>1 且含非只读 → 拦截
    if len(infos) > 1 and any(i.kind != "read" for i in infos):
        results.append(RuleResult(
            rule="multi-statement",
            verdict=Verdict.BLOCK,
            tier=Tier.UNKNOWN,
            reason="多语句批处理含非只读操作——MVP 禁止批量写，请拆成单条执行。",
        ))

    for info in infos:
        results.extend(_rules_for(info, origin))
    return results


_SEVERITY = {Verdict.ALLOW: 0, Verdict.REVIEW: 1, Verdict.BLOCK: 2}


def aggregate(infos: list[StatementInfo], rules: list[RuleResult]):
    """汇总规则结果 → Assessment。"""
    worst = max((r.verdict for r in rules), key=lambda v: _SEVERITY[v], default=Verdict.ALLOW)
    # tier 取最严重的非读语句；否则 read
    tier = Tier.READ
    for i in infos:
        if i.kind == "dml":
            tier = Tier.DML
            break
        if i.kind == "ddl":
            tier = Tier.DDL
            break
    if worst != Verdict.ALLOW:
        for r in rules:
            if r.verdict == worst:
                tier = r.tier or tier
                break

    reasons = [r.reason for r in rules if r.verdict == worst] or [
        r.reason for r in rules if r.verdict != Verdict.ALLOW
    ]
    primary = next((i for i in infos if i.kind != "read"), infos[0] if infos else None)

    tables: list[str] = []
    for i in infos:
        for t in i.tables:
            if t not in tables:
                tables.append(t)

    return {
        "verdict": worst,
        "tier": tier,
        "reasons": reasons[:3],
        "rules": rules,
        "tables": tables,
        "has_where": primary.has_where if primary else None,
        "has_limit": primary.has_limit if primary else None,
        "parse_error": primary.parse_error if primary else None,
        "is_multi": len(infos) > 1,
    }
