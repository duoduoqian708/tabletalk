# backend/AGENTS.md

Backend = FastAPI sidecar (`backend/app`). It owns ALL DB connections, the safety
gate, and AI orchestration. Frontend never touches a DB directly.

## Entry & lifecycle

- `app/main.py::create_app` — CORS wide-open (`["*"]` + credentials), `sidecar_token_guard`
  middleware (only `/api/v1/health`, `/api/v1/bootstrap`, `OPTIONS`, and non-`/api/*`
  are exempt). `lifespan` seeds `data_dir/demo.db` if absent (failure is swallowed),
  and closes pools on shutdown.
- SPA is mounted at `/` from `env.web_dist` (default `../frontend/dist`) when present.

## Safety gate (`app/safety/`, pure + fully unit-tested)

Verdicts: `ALLOW` | `REVIEW` | `BLOCK`. One gate for AI + manual SQL.
- `parser.py` uses sqlglot; parse failure → `kind="unknown"` (never silently passes).
- `rules.py`: parse-failure/unknown → REVIEW; DDL+AI → BLOCK, DDL+manual → REVIEW;
  UPDATE/DELETE w/o WHERE → BLOCK; other DML → REVIEW; multi-statement w/ any write → BLOCK.
- `REVIEW` returns `preview_rows` (COUNT, 3s timeout) and needs `confirm:true` to run.
- **INERT:** `CLEARED_GATE_REVIEW_THRESHOLD` and `gate_rules` are persisted & returned by
  `GET /settings` but never read by the gate. Writes are always REVIEW.
- Read-only connection (`read_only=True`) hard-BLOCKS any non-ALLOW verdict in `api/query.py`.

## AI (`app/ai/`)

- 5 tools: `get_schema`, `describe_table`, `run_query` (read), `run_dml` (write),
  `draft_ddl` (**draft only, never executes**). No DDL exec tool by design.
- `gateway.is_effective_mock`: `mock`, OR `cloud` with no `api_key` → silent mock downgrade.
  `POST /ai/test` disables the fallback to surface real errors.
- `loop.py`: function-calling loop, `MAX_TURNS=6`, SSE events think/sql_card/text/done.
  Report mode (`report.py`) forces `include_data=true` for aggregates only.
- Default provider is `mock` → full offline flow with deterministic output.

## Core (`app/core/`)

- `connections.py`: creds stored **plaintext** in `data_dir/connections.json`
  (`credential_ref` reserved for future keychain). `public()` masks password.
- `pool.py`: SQLite pool size 1 (serialized); PG/MySQL = `CLEARED_POOL_SIZE` (default 3);
  auto-reconnect once on error.
- `query.py`: `SELECT` w/o LIMIT gets `LIMIT (max_rows+1)` injected at SQL layer
  (`CLEARED_QUERY_MAX_ROWS` default 1000); big ints stringified for JSON (JS precision).
- `schema.py`: 30s cache; pass `?refresh=true` after structural changes.
- Dialects: `sqlite`/`postgres`/`mysql` via `DialectAdapter` registry; add by
  implementing adapter + importing it in `app/core/dialects/__init__.py`.

## Knowledge (`app/knowledge/`)

- Lazy build once per process; artifact `data_dir/knowledge-{conn_id}.json` reloaded on restart.
- `route_tables` uses **confirmed tags only** — draft tags don't affect routing until confirmed.
- Default embedder = `HashingEmbedder` (offline, DIM 256); `api` provider → OpenAI-compatible `/embeddings`.

## Data dir (`CLEARED_DATA_DIR`, default `~/.cleared`)

`connections.json` · `settings.json` · `sidecar.token` (chmod 600) · `audit.log` (JSONL) ·
`chat.db` (SQLite sessions) · `knowledge.json` + `knowledge-{conn_id}.json` · `demo.db`.

## Tests

`pytest.ini` sets `asyncio_mode=auto` (no decorator on `async def` tests).
Full suite SQLite-only: `cd backend && .venv/bin/python -m pytest -q`.
Integration (`tests/integration`, PG/MySQL) Docker-gated: `CLEARED_INTEGRATION=1 .venv/bin/python -m pytest tests/integration -v`.
