# Contributing to TableTalk

## Quick start

```bash
python3 backend/tabletalk.py --check   # env check (venv/deps/frontend)
cd backend && .venv/bin/python -m pytest -q
cd frontend && npm run typecheck && npm run build
```

## Development handbook (single source of truth)

`docs/product-handbook/` is the only dev blueprint. Before any task, read its `README.md` → "给 AI 助手的操作指引" (also applies to humans): per-task read `02-feature-specs.md` entry, check `03/04/05/07` for relevant sections, follow hard rules in `C` and roadmap in `06`.

- Colors: only `frontend/src/renderer/src/styles/tokens.css` variables, no hardcoded hex.
- Safety gate: `backend/app/safety/` is pure local rules; AI toolset has no DDL exec tool (only `draft_ddl`); parse failures default to REVIEW.
- Tests: backend `pytest` must be green before merge; frontend `typecheck && build` gate; star-graph changes need Playwright pixel-assertion e2e (`frontend/scripts/verify-graph3d.mjs`).

## Branch & PR

- Branch from `main`, keep commits scoped.
- Run relevant tests before push (changed package → its tests).
- Do not edit `docs/product-handbook/` without a matching `02` entry and acceptance criteria.

## Reporting security issues

See `SECURITY.md` (private disclosure, no public issue for vulns).

## License

Apache-2.0. Brand name & logo are trademarks of the project (not covered by Apache-2.0).
