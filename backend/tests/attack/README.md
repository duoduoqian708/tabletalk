# Attack dataset skeleton (F2)

This is the skeleton for the public gate attack set (`tabletalk-gate`).

- Goal: ≥100 malicious SQLs, 100% BLOCK/REVIEW, CI badge. Each case carries expected verdict + rule_id + reason (A1 "拦截可解释").
- Layout: `cases.yaml` → list of `{id, sql, dialect, origin, expect_verdict, expect_rule, description}`.
- Runner: `tests/safety/test_gate_attack.py` loads YAML, calls `assess_sql`, asserts verdict & rule.
- CI: badge counts `passed/total`; any case flipping from BLOCK to ALLOW fails CI.

Categories (to fill to 100):
- no-WHERE UPDATE/DELETE (R3)
- DDL via AI (R2)
- multi-statement mixed write (R7)
- comment-obfuscation (`/**/`, `--`, `;` injection)
- stacked queries & piggyback
- privilege escalation (GRANT, SET ROLE)
- TCL abuse

Current: placeholder (5 samples) — expand during Phase 1 F2.
