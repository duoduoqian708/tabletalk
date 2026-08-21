"""风险规则引擎：StatementInfo → RuleResult（A1 结构化 + 双语 + 对象）。

闸门与 AI 无关：AI 生成和手动执行的 SQL 走同一套规则。
"""
from __future__ import annotations

from .models import Origin, RuleResult, Tier, Verdict
from .parser import StatementInfo

# 双语消息表（走 i18n 词典，key 与前端 gate.rule.* 对齐）
_MSGS: dict[str, tuple[str, str]] = {
    "parse-failure": (
        "无法可靠解析该语句，按写操作对待，需确认。",
        "Failed to reliably parse the statement; treat as write and require confirmation.",
    ),
    "ddl-ai": (
        "AI 没有 DDL 工具，无法执行 DDL。请将脚本发送到编辑器手动运行。",
        "AI has no DDL tool; DDL cannot be executed. Send the script to the editor for manual run.",
    ),
    "ddl-manual": (
        "DDL 属于高危操作，需手动强确认。",
        "DDL is high-risk and requires strong manual confirmation.",
    ),
    "dml-no-where": (
        "UPDATE/DELETE 缺少 WHERE 条件——这会是全表写，已拦截。请加 WHERE 或先 SELECT 确认目标。",
        "UPDATE/DELETE missing WHERE — would be full-table write, blocked. Add WHERE or SELECT to confirm targets first.",
    ),
    "dml-confirm": (
        "写操作需确认。已估算影响行数，请核对后执行。",
        "Write operation needs confirmation. Affected rows estimated — verify before execution.",
    ),
    "tcl-confirm": (
        "事务控制语句需确认。",
        "Transaction control statement requires confirmation.",
    ),
    "read-no-limit": (
        "只读放行。未加 LIMIT，若表很大建议加 LIMIT 控制返回量。",
        "Read allowed. No LIMIT; add LIMIT for large tables.",
    ),
    "read-allow": (
        "只读语句，直接放行。",
        "Read statement, allowed.",
    ),
    "unknown-fallback": (
        "无法识别的语句，需确认。",
        "Unrecognized statement, requires confirmation.",
    ),
    "multi-statement": (
        "多语句批处理含非只读操作——MVP 禁止批量写，请拆成单条执行。",
        "Multi-statement batch contains non-read ops — batch writes are blocked, split into single statements.",
    ),
}


def _msg(rule: str) -> tuple[str, str]:
    return _MSGS.get(rule, ("", ""))


def _rules_for(info: StatementInfo, origin: Origin) -> list[RuleResult]:
    out: list[RuleResult] = []
    tables = list(info.tables) if info.tables else []

    # R1 解析失败/未知 → 按写处理，永不 ALLOW
    if info.parse_error or info.kind == "unknown":
        zh, en = _msg("parse-failure")
        # 细节：把解析错误/未知类型拼到中文里，保留可读性
        zh_detail = f"{zh}（{info.parse_error or '未知类型'}）"
        en_detail = f"{en} ({info.parse_error or 'unknown'})"
        out.append(RuleResult(
            rule="parse-failure",
            verdict=Verdict.REVIEW,
            tier=Tier.UNKNOWN,
            reason=zh_detail,
            reason_en=en_detail,
            objects=tables or [info.parse_error or "unknown"][:1],
        ))
        return out

    # R2 DDL：AI 物理上无 DDL 工具，此路径不应发生 → 硬拦截
    if info.kind == "ddl":
        if origin == Origin.AI:
            zh, en = _msg("ddl-ai")
            out.append(RuleResult(
                rule="ddl-ai",
                verdict=Verdict.BLOCK,
                tier=Tier.DDL,
                reason=zh,
                reason_en=en,
                objects=tables,
            ))
        else:
            zh, en = _msg("ddl-manual")
            out.append(RuleResult(
                rule="ddl-manual",
                verdict=Verdict.REVIEW,
                tier=Tier.DDL,
                reason=zh,
                reason_en=en,
                objects=tables,
            ))
        return out

    # R3 UPDATE/DELETE 无 WHERE → 拦截
    if info.stmt_type in ("update", "delete") and not info.has_where:
        zh, en = _msg("dml-no-where")
        out.append(RuleResult(
            rule="dml-no-where",
            verdict=Verdict.BLOCK,
            tier=Tier.DML,
            reason=zh,
            reason_en=en,
            objects=tables,
        ))
        return out

    # R4 其余 DML → 需确认
    if info.kind == "dml":
        zh, en = _msg("dml-confirm")
        out.append(RuleResult(
            rule="dml-confirm",
            verdict=Verdict.REVIEW,
            tier=Tier.DML,
            reason=zh,
            reason_en=en,
            objects=tables,
        ))
        return out

    # R5 事务控制 → 需确认
    if info.kind == "tcl":
        zh, en = _msg("tcl-confirm")
        out.append(RuleResult(
            rule="tcl-confirm",
            verdict=Verdict.REVIEW,
            tier=Tier.UNKNOWN,
            reason=zh,
            reason_en=en,
            objects=tables,
        ))
        return out

    # R6 读 → 放行
    if info.kind == "read":
        if info.stmt_type == "select" and not info.has_limit:
            zh, en = _msg("read-no-limit")
            out.append(RuleResult(
                rule="read-no-limit",
                verdict=Verdict.ALLOW,
                tier=Tier.READ,
                reason=zh,
                reason_en=en,
                objects=tables,
            ))
        else:
            zh, en = _msg("read-allow")
            out.append(RuleResult(
                rule="read-allow",
                verdict=Verdict.ALLOW,
                tier=Tier.READ,
                reason=zh,
                reason_en=en,
                objects=tables,
            ))
        return out

    # 兜底
    zh, en = _msg("unknown-fallback")
    out.append(RuleResult(
        rule="unknown-fallback",
        verdict=Verdict.REVIEW,
        tier=Tier.UNKNOWN,
        reason=zh,
        reason_en=en,
        objects=tables,
    ))
    return out


def run_rules(infos: list[StatementInfo], origin: Origin) -> list[RuleResult]:
    results: list[RuleResult] = []

    # R7 多语句批处理：>1 且含非只读 → 拦截
    if len(infos) > 1 and any(i.kind != "read" for i in infos):
        zh, en = _msg("multi-statement")
        # 合并所有表的对象
        all_tables: list[str] = []
        for i in infos:
            for t in i.tables:
                if t not in all_tables:
                    all_tables.append(t)
        results.append(RuleResult(
            rule="multi-statement",
            verdict=Verdict.BLOCK,
            tier=Tier.UNKNOWN,
            reason=zh,
            reason_en=en,
            objects=all_tables,
        ))

    for info in infos:
        results.extend(_rules_for(info, origin))
    return results


_SEVERITY = {Verdict.ALLOW: 0, Verdict.REVIEW: 1, Verdict.BLOCK: 2}


def aggregate(infos: list[StatementInfo], rules: list[RuleResult]):
    """汇总规则结果 → Assessment（含结构化 reasons）。"""
    worst = max((r.verdict for r in rules), key=lambda v: _SEVERITY[v], default=Verdict.ALLOW)
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

    # 结构化 reasons：按最严重 verdict 聚合，保留 rule_id/message/objects
    picked = [r for r in rules if r.verdict == worst] or [r for r in rules if r.verdict != Verdict.ALLOW]
    reasons = [
        {
            "rule_id": r.rule,
            "message": r.reason,
            "message_en": r.reason_en,
            "objects": list(r.objects),
        }
        for r in picked[:3]
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
        "reasons": reasons,
        "rules": rules,
        "tables": tables,
        "has_where": primary.has_where if primary else None,
        "has_limit": primary.has_limit if primary else None,
        "parse_error": primary.parse_error if primary else None,
        "is_multi": len(infos) > 1,
    }
