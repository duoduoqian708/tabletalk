"""策略即配置：表级/模式级覆盖（A2）。

旧实现内联在 api/query.py（仅手动路径生效），下沉为纯同步函数后，
手动查询、AI 工具、审批流、lint 四方同源生效 —— 同一策略（如"表 X 禁止写"）
对 AI 与手动一致，防绕过。语义逐条对齐旧行为：allow 不放松已判定、命中收紧 break、
模式级仅在仍为 REVIEW 时触发、任何异常静默回退原评估（可用性优先）。
"""
from __future__ import annotations

from app.safety.models import Assessment, Verdict


def _verdict_of(act: str) -> Verdict:
    return Verdict.BLOCK if act == "block" else Verdict.REVIEW if act == "review" else Verdict.ALLOW


def apply_policy(assessment: Assessment, policy, sql: str) -> Assessment:
    """对一次评估应用策略覆盖，返回同一 Assessment（就地修改）。policy 失败/为空则原样返回。"""
    if policy is None:
        return assessment
    try:
        table_rules = getattr(policy, "table_rules", None) or {}
        # 表级：最高优先生效（allow 不放松已判定；命中收紧即 break）
        for tbl in list(assessment.tables):
            act = table_rules.get(tbl) or table_rules.get(tbl.lower())
            if not act:
                continue
            act = str(act).lower()
            ver = _verdict_of(act)
            if ver == assessment.verdict:
                continue
            reason = {
                "rule_id": f"policy-table-{tbl}",
                "message": f"命中策略 v{getattr(policy, 'version', 1)}：表 “{tbl}” 规则 {act.upper()}",
                "message_en": f"Policy v{getattr(policy, 'version', 1)}: table '{tbl}' {act.upper()}",
                "objects": [tbl],
            }
            if act != "allow":  # allow 不放宽：非 ALLOW 时不改写、不 break
                assessment.verdict = ver
                assessment.reasons = [reason] + [r for r in assessment.reasons if r.get("rule_id") != f"policy-table-{tbl}"]
                break
        # 模式级：仅当仍为 REVIEW 才触发（避免已 BLOCK 被降级）
        if assessment.verdict == Verdict.REVIEW and assessment.tables:
            for pr in getattr(policy, "pattern_rules", []) or []:
                if pr.get("id") == "delete-requires-time" and "delete" in sql.lower():
                    has_time = any(kw in sql.lower() for kw in ("created_at", "updated_at", "time", "date", "timestamp"))
                    if not has_time:
                        reason = {
                            "rule_id": "policy-pattern-delete-time",
                            "message": f"命中策略 v{getattr(policy, 'version', 1)}：DELETE 必须 WHERE 带时间范围",
                            "message_en": f"Policy v{getattr(policy, 'version', 1)}: DELETE requires time predicate",
                            "objects": assessment.tables,
                        }
                        assessment.reasons = [reason] + assessment.reasons
                        assessment.verdict = Verdict.BLOCK
                        break
    except Exception:  # noqa: BLE001  策略异常不阻断查询
        pass
    return assessment