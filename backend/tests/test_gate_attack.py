"""Skeleton runner for attack dataset — Phase 0 placeholder (5 cases). Full 100+ in Phase 1 F2."""
from __future__ import annotations

import pathlib

import yaml  # type: ignore

from app.safety.gate import assess_sql, sqlglot_dialect_for
from app.safety.models import Origin

CASES = pathlib.Path(__file__).parent / "attack" / "cases.yaml"


def _load():
    if not CASES.exists():
        return []
    data = yaml.safe_load(CASES.read_text(encoding="utf-8")) or []
    return data


def test_attack_skeleton():
    cases = _load()
    assert len(cases) >= 100, f"attack set must have ≥100, got {len(cases)}"
    for c in cases:
        origin = Origin.AI if c.get("origin") == "ai" else Origin.MANUAL
        a = assess_sql(c["sql"], sqlglot_dialect_for(c.get("dialect", "sqlite")), origin)
        assert a.verdict.value == c["expect_verdict"], f"{c['id']} verdict {a.verdict.value} != {c['expect_verdict']}"
        assert any(r.rule == c["expect_rule"] for r in a.rules), f"{c['id']} missing rule {c['expect_rule']}"
