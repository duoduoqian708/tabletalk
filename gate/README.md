# tabletalk-gate

Pure, model-independent safety gate for TableTalk — local rule engine, offline capable, zero-dependency (except `sqlglot`).

```python
from tabletalk_gate import assess

a = assess("UPDATE orders SET status='paid'", dialect="sqlite", origin="ai")
print(a.verdict)   # "block"
print(a.reasons)   # [{"rule_id": "dml-no-where", "message": "...", "objects": ["orders"]}]
```

- **Three tiers**: `ALLOW` (read), `REVIEW` (DML/DDL manual), `BLOCK` (danger)
- **Zero network**: pure `sqlglot` parsing + local rules, works offline
- **Bilingual reasons**: `message` (zh) + `message_en` (en) + `objects` (tables/columns)
- **Attack dataset**: `tests/attack/cases.yaml` — 100 malicious SQLs, 100% blocked, CI badge

See main repo `backend/app/safety/` for the canonical implementation (this package is a pure copy for independent distribution).
