"""风险规则引擎：StatementInfo → RuleResult（A1 结构化 + 双语 + 对象）。

闸门与 AI 无关：AI 生成和手动执行的 SQL 走同一套规则。
"""
from __future__ import annotations

from app.safety.models import Origin, RuleResult, Tier, Verdict
from app.safety.parser import StatementInfo

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
    "scheduled-insert": (
        "定时任务写入（INSERT）已放行，将记入审计。",
        "Scheduled INSERT allowed; will be audited.",
    ),
    "ddl-scheduled": (
        "定时任务禁止执行 DDL 结构变更。",
        "Scheduled tasks cannot execute DDL.",
    ),
}


def _msg(rule: str) -> tuple[str, str]:
    return _MSGS.get(rule, ("", ""))


# ── 规则目录元数据（闸门配置面 & GET /safety/rules 共享） ─────────────────
# floor=True = 硬规则：只允许收严、不可放宽（默认值已是安全下限）。
RULE_META: dict[str, dict[str, object]] = {
    "parse-failure":    {"tier": "unknown", "scope": "any",           "default": "review", "floor": True},
    "ddl-ai":           {"tier": "ddl",     "scope": "ddl",           "default": "block",  "floor": True},
    "ddl-manual":       {"tier": "ddl",     "scope": "ddl",           "default": "review", "floor": False},
    "dml-no-where":     {"tier": "dml",     "scope": "update/delete", "default": "block",  "floor": True},
    "dml-confirm":      {"tier": "dml",     "scope": "dml",           "default": "review", "floor": False},
    "tcl-confirm":      {"tier": "unknown", "scope": "tcl",           "default": "review", "floor": False},
    "read-no-limit":    {"tier": "read",    "scope": "select",        "default": "allow",  "floor": False},
    "read-allow":       {"tier": "read",    "scope": "read",          "default": "allow",  "floor": False},
    "unknown-fallback": {"tier": "unknown", "scope": "any",           "default": "review", "floor": True},
    "multi-statement":  {"tier": "unknown", "scope": "batch",         "default": "block",  "floor": True},
    "scheduled-insert": {"tier": "dml",     "scope": "insert",        "default": "allow",  "floor": False},
    "ddl-scheduled":    {"tier": "ddl",     "scope": "ddl",           "default": "block",  "floor": True},
}

# 严格度阶梯：只允许向更严方向覆盖（同级 no-op，放宽一律忽略）
_LADDER: dict[Verdict, int] = {Verdict.ALLOW: 0, Verdict.REVIEW: 1, Verdict.BLOCK: 2}


def normalize_gate_rules(rules: object) -> dict[str, str]:
    """契约收口（settings 校验与引擎共用，单一来源）：仅保留「已知规则 + 合法判定 +
    严格更严」的覆盖。同级/放宽/未知规则/旧 bool 历史数据一律丢弃。"""
    if not isinstance(rules, dict):
        return {}
    out: dict[str, str] = {}
    for rid, val in rules.items():
        if not isinstance(rid, str) or not isinstance(val, str):
            continue
        meta = RULE_META.get(rid)
        if meta is None:
            continue
        v = val.lower()
        if v not in {x.value for x in Verdict}:
            continue
        if _LADDER[Verdict(v)] <= _LADDER[Verdict(str(meta["default"]))]:
            continue  # 同级/放宽 → 丢弃（floor 规则自然不可放宽）
        out[rid] = v
    return out


def _eff_verdict(rule_id: str, default: Verdict, overrides: dict | None) -> tuple[Verdict, bool]:
    """应用规则覆盖。返回 (生效判定, 是否被覆盖收严)。"""
    raw = (overrides or {}).get(rule_id)
    if not raw:
        return default, False
    raw = str(raw).lower()
    if raw not in {v.value for v in Verdict}:
        return default, False
    target = Verdict(raw)
    if _LADDER[target] > _LADDER[default]:
        return target, True  # 严格更严 → 生效
    return default, False     # 同级/放宽 → 忽略


def _mk(rule_id: str, default: Verdict, tier: Tier, objects: list[str], overrides: dict | None,
        detail: str | None = None) -> RuleResult:
    """统一构造 RuleResult：应用阶梯覆盖，并在 reason 中留痕 override 来源（A1 可解释）。"""
    verdict, applied = _eff_verdict(rule_id, default, overrides)
    zh, en = _msg(rule_id)
    if applied:
        zh += f"（配置收严 → {verdict.value}）"
        en += f" (configured stricter -> {verdict.value})"
    if detail:
        zh += f"（{detail}）"
        en += f" ({detail})"
    return RuleResult(rule=rule_id, verdict=verdict, tier=tier, reason=zh, reason_en=en, objects=objects)


def _rules_for(info: StatementInfo, origin: Origin, overrides: dict | None = None) -> list[RuleResult]:
    out: list[RuleResult] = []
    tables = list(info.tables) if info.tables else []
    ov = overrides or None

    # R1 解析失败/未知 → 按写处理，永不 ALLOW
    if info.parse_error or info.kind == "unknown":
        out.append(_mk("parse-failure", Verdict.REVIEW, Tier.UNKNOWN,
                       tables or [info.parse_error or "unknown"][:1], ov,
                       detail=info.parse_error or "未知类型"))
        return out

    # R2 DDL：AI/定时任务 无 DDL 工具 → 硬拦截；手动 → 强确认
    if info.kind == "ddl":
        if origin == Origin.AI:
            out.append(_mk("ddl-ai", Verdict.BLOCK, Tier.DDL, tables, ov))
        elif origin == Origin.SCHEDULED:
            out.append(_mk("ddl-scheduled", Verdict.BLOCK, Tier.DDL, tables, ov))
        else:
            out.append(_mk("ddl-manual", Verdict.REVIEW, Tier.DDL, tables, ov))
        return out

    # R3 UPDATE/DELETE 无 WHERE → 拦截
    if info.stmt_type in ("update", "delete") and not info.has_where:
        out.append(_mk("dml-no-where", Verdict.BLOCK, Tier.DML, tables, ov))
        return out

    # R4 其余 DML → 需确认；定时任务仅放行 INSERT（其余 DML 无人确认即不执行）
    if info.kind == "dml":
        if origin == Origin.SCHEDULED and info.stmt_type == "insert":
            out.append(_mk("scheduled-insert", Verdict.ALLOW, Tier.DML, tables, ov))
        else:
            out.append(_mk("dml-confirm", Verdict.REVIEW, Tier.DML, tables, ov))
        return out

    # R5 事务控制 → 需确认
    if info.kind == "tcl":
        out.append(_mk("tcl-confirm", Verdict.REVIEW, Tier.UNKNOWN, tables, ov))
        return out

    # R6 读 → 放行
    if info.kind == "read":
        if info.stmt_type == "select" and not info.has_limit:
            out.append(_mk("read-no-limit", Verdict.ALLOW, Tier.READ, tables, ov))
        else:
            out.append(_mk("read-allow", Verdict.ALLOW, Tier.READ, tables, ov))
        return out

    # 兜底
    out.append(_mk("unknown-fallback", Verdict.REVIEW, Tier.UNKNOWN, tables, ov))
    return out


def run_rules(infos: list[StatementInfo], origin: Origin, overrides: dict | None = None) -> list[RuleResult]:
    results: list[RuleResult] = []
    ov = normalize_gate_rules(overrides) or None

    # R7 多语句批处理：>1 且含非只读 → 拦截
    if len(infos) > 1 and any(i.kind != "read" for i in infos):
        # 合并所有表的对象
        all_tables: list[str] = []
        for i in infos:
            for t in i.tables:
                if t not in all_tables:
                    all_tables.append(t)
        results.append(_mk("multi-statement", Verdict.BLOCK, Tier.UNKNOWN, all_tables, ov))

    for info in infos:
        results.extend(_rules_for(info, origin, ov))
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
