#!/usr/bin/env bash
# Sync gate package from canonical backend/app/safety -> gate/tabletalk_gate
# Usage: ./scripts/sync-gate.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/backend/app/safety"
DST="$ROOT/gate/tabletalk_gate"
echo "Sync $SRC -> $DST"
rsync -av --delete --exclude '__pycache__' --exclude '*.pyc' --exclude '__init__.py' "$SRC/" "$DST/"
# Patch to standalone: relative imports + simple sqlglot_dialect_for
for f in "$DST/rules.py" "$DST/gate.py" "$DST/parser.py" "$DST/models.py" "$DST/blast.py"; do
  [ -f "$f" ] || continue
  # app.safety -> .
  sed -i '' 's/from app\.safety/from ./g' "$f" 2>/dev/null || sed -i 's/from app\.safety/from ./g' "$f"
  sed -i '' 's/import app\.safety/import ./g' "$f" 2>/dev/null || sed -i 's/import app\.safety/import ./g' "$f"
done
# gate.py: make sqlglot_dialect_for standalone (no app.core registry)
if grep -q "from app.core.dialects.registry" "$DST/gate.py"; then
  # replace the whole function with passthrough
  cat > /tmp/gate_patch.py <<'PY'
def sqlglot_dialect_for(dialect: str) -> str:
    return dialect or "sqlite"
PY
  # Use python to replace the function
  python3 <<'PYEOF'
import pathlib, re
p = pathlib.Path("gate/tabletalk_gate/gate.py")
t = p.read_text()
# replace the sqlglot_dialect_for function
t = re.sub(r"def sqlglot_dialect_for\(dialect: str\) -> str:.*?return \"sqlite\".*?# 未注册.*", "def sqlglot_dialect_for(dialect: str) -> str:\n    return dialect or \"sqlite\"", t, flags=re.S)
p.write_text(t)
print("patched gate.py sqlglot_dialect_for")
PYEOF
fi
echo "Done. Remember to rebuild: pip install -e gate"
