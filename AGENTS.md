# AGENTS.md

Compact guidance for OpenCode sessions in this repo. Full product/architecture
context lives in `CLAUDE.md` — read it first. This file is only the non-obvious,
easy-to-miss, verified facts.

## Layout (two packages, one served app)

- `backend/` — Python + FastAPI sidecar. Owns all DB connections, the safety
  gate, and AI orchestration. The single source of truth for DB access.
- `frontend/` — React + TypeScript + Vite SPA. **Vite root is `src/renderer/`**
  (not repo root) and build output is `frontend/dist/` (Vite `outDir: ../../dist`
  relative to `src/renderer`). The backend serves that dist same-origin at `/`.
- The frontend is NOT a separate server in prod: build it (`frontend/dist/`) and
  the backend serves it. Dev uses Vite on :5173 proxying `/api` → `127.0.0.1:8765`.

## Backend commands

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python scripts/seed_demo_db.py     # creates ~/.cleared/demo.db demo DB
.venv/bin/python -m uvicorn app.main:app --reload --port 8765
.venv/bin/python -m pytest -q                # full suite (~116, SQLite only)
```

- `pytest.ini` sets `asyncio_mode = auto` → `async def` tests need no decorator.
- Single test: `.venv/bin/python -m pytest tests/safety/test_gate.py::<name> -q`
- Integration tests (`tests/integration/`, Postgres/MySQL) are **Docker-gated**:
  `docker compose -f docker-compose.integration.yml up -d && CLEARED_INTEGRATION=1 .venv/bin/python -m pytest tests/integration -v`. They are skipped without the env var.

## Frontend commands

```bash
cd frontend
npm install
npm run dev        # Vite :5173, proxies /api to backend — backend must be running
npm run typecheck  # tsc --noEmit
npm run build      # emits frontend/dist/, served by backend
npm run screenshot # Playwright: starts backend → connects demo DB → knowledge review → AI tags
```

Aliases: `@renderer` → `src/renderer/src`, `@shared` → `src/shared`.

## Auth gotcha (easy to waste time on)

All `/api/*` routes (except `GET /health`, `GET /bootstrap`, and OPTIONS) require
header `X-Cleared-Token`. The token is set by `CLEARED_SIDECAR_TOKEN`, or
auto-generated into `<data_dir>/sidecar.token`. When calling the API from scripts
or tests, first `GET /api/v1/bootstrap` (unauthenticated) to obtain the token.
Browser does this via `src/renderer/src/hooks/useBootstrap.ts`.

## Safety gate — hard invariants

- `app/safety/` is pure, model-independent rules logic, fully unit-tested under
  `tests/safety/`. AI-generated AND manually-run SQL both pass one gate.
- DDL (CREATE/ALTER/DROP/TRUNCATE) is **manual-only**. The AI toolset has NO DDL
  exec tool — only `draft_ddl` (returns a script, never executes). Don't add one.
- Parse failures default to "write" (REVIEW), never ALLOW.

## AI gateway

Default `CLEARED_AI_PROVIDER=mock` needs no API key and runs the full flow.
`cloud`/`local` use any OpenAI-compatible endpoint (`CLEARED_AI_BASE_URL/API_KEY/MODEL`).

## First-run behavior

Backend lifespan seeds `data_dir/demo.db` on first start; the connection page
"使用演示库" points at it. `CLEARED_DATA_DIR` (default `~/.cleared`) holds
connections, audit JSONL, and the token.

## Config

Backend env via `backend/.env` (see `.env.example`): `CLEARED_PORT`,
`CLEARED_DATA_DIR`, `CLEARED_SIDECAR_TOKEN`, `CLEARED_AI_*`,
`CLEARED_WEB_DIST` (SPA dir, default `../frontend/dist`),
`CLEARED_POOL_SIZE`, `CLEARED_QUERY_MAX_ROWS`. Runtime AI/gate settings are
overridable via `PUT /api/v1/settings`.

> **Gotcha:** `CLEARED_GATE_REVIEW_THRESHOLD` and `gate_rules` are configurable
> and persisted, but **no gate code reads them** — writes are always `REVIEW`
> regardless of row count. Don't assume a threshold auto-allows writes.
