"""策略即配置（apply_policy）单元测试：表级/模式级覆盖语义（A2，纯同步、防绕过）。"""
from __future__ import annotations

from app.core.settings import Policy
from app.safety import gate, policy
from app.safety.models import Origin, Verdict


def _assess(sql, origin=Origin.MANUAL, overrides=None):
    return gate.assess_sql(sql, "sqlite", origin, overrides=overrides)


def _apply(a, pol, sql):
    return policy.apply_policy(a, pol, sql)


def test_table_rule_block():
    pol = Policy(table_rules={"orders": "block"})
    out = _apply(_assess("UPDATE orders SET x=1 WHERE id=1"), pol, "UPDATE orders SET x=1 WHERE id=1")
    assert out.verdict == Verdict.BLOCK
    assert any(r.get("rule_id") == "policy-table-orders" for r in out.reasons)


def test_table_rule_review():
    pol = Policy(table_rules={"orders": "review"})
    out = _apply(_assess("SELECT * FROM orders"), pol, "SELECT * FROM orders")
    assert out.verdict == Verdict.REVIEW


def test_table_rule_allow_does_not_relax():
    # 策略标 allow 不放松已判定（dml-confirm=review 原地保留）
    pol = Policy(table_rules={"orders": "allow"})
    out = _apply(_assess("UPDATE orders SET x=1 WHERE id=1", Origin.MANUAL), pol, "UPDATE orders SET x=1 WHERE id=1")
    assert out.verdict == Verdict.REVIEW


def test_table_rule_case_insensitive():
    # key 保持小写，SQL 表名大小写均命中（apply_policy 用小写化后的表名查 key）
    pol = Policy(table_rules={"orders": "block"})
    out = _apply(_assess("DELETE FROM ORDERS WHERE id=1"), pol, "DELETE FROM ORDERS WHERE id=1")
    assert out.verdict == Verdict.BLOCK


def test_table_rule_block_wins_over_read():
    pol = Policy(table_rules={"orders": "block"})
    out = _apply(_assess("SELECT * FROM orders"), pol, "SELECT * FROM orders")
    assert out.verdict == Verdict.BLOCK


def test_pattern_delete_requires_time():
    pol = Policy(pattern_rules=[{"id": "delete-requires-time"}])
    out = _apply(_assess("DELETE FROM orders WHERE id=1"), pol, "DELETE FROM orders WHERE id=1")
    assert out.verdict == Verdict.BLOCK
    assert any(r.get("rule_id") == "policy-pattern-delete-time" for r in out.reasons)
    # 带时间谓词的 DELETE 命中模式 → 不追加、保持 review
    out2 = _apply(_assess("DELETE FROM orders WHERE created_at < '2026-01-01'"), pol, "DELETE FROM orders WHERE created_at < '2026-01-01'")
    assert out2.verdict == Verdict.REVIEW
    assert not any(r.get("rule_id") == "policy-pattern-delete-time" for r in out2.reasons)


def test_pattern_only_on_review_tier():
    # 已 BLOCK（无 WHERE）不再被模式级降级/覆盖
    pol = Policy(pattern_rules=[{"id": "delete-requires-time"}])
    out = _apply(_assess("DELETE FROM orders"), pol, "DELETE FROM orders")
    assert out.verdict == Verdict.BLOCK
    assert not any(r.get("rule_id") == "policy-pattern-delete-time" for r in out.reasons)


def test_rule_override_and_policy_compose():
    # 规则覆盖把读升到 review（read-no-limit），表策略再把该表升到 block → 最严生效，两条 reason 并存
    pol = Policy(table_rules={"orders": "block"})
    out = _apply(_assess("SELECT * FROM orders", overrides={"read-no-limit": "review"}),
                 pol, "SELECT * FROM orders")
    assert out.verdict == Verdict.BLOCK
    assert any(r.get("rule_id") == "read-no-limit" for r in out.reasons)
    assert any(r.get("rule_id") == "policy-table-orders" for r in out.reasons)


def test_policy_none_passthrough():
    out = _apply(_assess("SELECT * FROM orders"), None, "SELECT * FROM orders")
    assert out.verdict == Verdict.ALLOW


def test_malformed_policy_tolerated():
    # 畸形策略不阻断查询（apply_policy 静默回退原评估）
    class Bad:
        pass

    out = _apply(_assess("SELECT * FROM orders"), Bad(), "SELECT * FROM orders")
    assert out.verdict == Verdict.ALLOW