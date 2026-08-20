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
  the backend serves it. Dev uses Vite on :5173 proxying `/api` → `127.0.0.1:8777`.

## Backend commands

```bash
# 一键启动（从零到可用：自动建 venv → 装依赖 → 构建前端 → 起 sidecar → 开浏览器）
python3 backend/tabletalk.py        # 重复运行幂等秒起；--check 只检查环境

cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python scripts/seed_demo_db.py     # creates ~/.tabletalk/demo.db demo DB
.venv/bin/python -m uvicorn app.main:app --reload --port 8777
.venv/bin/python -m pytest -q                # full suite (~116, SQLite only)
```

- `pytest.ini` sets `asyncio_mode = auto` → `async def` tests need no decorator.
- Single test: `.venv/bin/python -m pytest tests/safety/test_gate.py::<name> -q`
- Integration tests (`tests/integration/`, Postgres/MySQL) are **Docker-gated**:
  `docker compose -f docker-compose.integration.yml up -d && TABLETALK_INTEGRATION=1 .venv/bin/python -m pytest tests/integration -v`. They are skipped without the env var.

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
header `X-TableTalk-Token`. The token is set by `TABLETALK_SIDECAR_TOKEN`, or
auto-generated into `<data_dir>/tabletalk.token`. When calling the API from scripts
or tests, first `GET /api/v1/bootstrap` (unauthenticated) to obtain the token.
Browser does this via `src/renderer/src/hooks/useBootstrap.ts`.

## Safety gate — hard invariants

- `app/safety/` is pure, model-independent rules logic, fully unit-tested under
  `tests/safety/`. AI-generated AND manually-run SQL both pass one gate.
- DDL (CREATE/ALTER/DROP/TRUNCATE) is **manual-only**. The AI toolset has NO DDL
  exec tool — only `draft_ddl` (returns a script, never executes). Don't add one.
- Parse failures default to "write" (REVIEW), never ALLOW.

## AI gateway

Default `TABLETALK_AI_PROVIDER=mock` needs no API key and runs the full flow.
`cloud`/`local` use any OpenAI-compatible endpoint (`TABLETALK_AI_BASE_URL/API_KEY/MODEL`).

## First-run behavior

Backend lifespan seeds `data_dir/demo.db` on first start; the connection page
"使用演示库" points at it. `TABLETALK_DATA_DIR` (default `~/.tabletalk`) holds
connections, audit JSONL, and the token.

## Config

Backend env via `backend/.env` (see `.env.example`): `TABLETALK_PORT`,
`TABLETALK_DATA_DIR`, `TABLETALK_SIDECAR_TOKEN`, `TABLETALK_AI_*`,
`TABLETALK_WEB_DIST` (SPA dir, default `../frontend/dist`),
`TABLETALK_POOL_SIZE`, `TABLETALK_QUERY_MAX_ROWS`. Runtime AI/gate settings are
overridable via `PUT /api/v1/settings`.

> **Gotcha:** `TABLETALK_GATE_REVIEW_THRESHOLD` and `gate_rules` are configurable
> and persisted, but **no gate code reads them** — writes are always `REVIEW`
> regardless of row count. Don't assume a threshold auto-allows writes.

## 会话死规矩（用户强制要求，务必自动执行，不等提醒）

- **改完后端（Python）代码后，必须主动重启后端 sidecar，不要等用户喊：**
  1. 找到占用端口的进程（默认 `TABLETALK_PORT=8777`）：`lsof -ti tcp:8777`；
  2. `kill -9 <pid>` 杀掉旧进程，确认端口空闲（`lsof -ti tcp:8777` 无输出）；
  3. 重新拉起：`cd backend && .venv/bin/python -m uvicorn app.main:app --reload --port 8777`；
  4. 验证：`curl -s http://127.0.0.1:8777/api/v1/health` 返回预期字段（尤其改了 health/settings 后要确认新字段出现，旧进程会返回旧结构导致前端显示 `—`）。
- **改完前端代码后，必须 `cd frontend && npm run typecheck && npm run build`**：后端 serve 的是 `frontend/dist`，不 rebuild 前端改动不生效。
- 任何改动收尾都要实际跑验证命令并确认输出，再向用户汇报结果。
- **执行写任务（改代码/文件）后，用精简表格汇报结果（用户强制要求，跨会话永久生效）**：
  仅在"写任务"完成后汇报；纯问答/讨论不需此格式。
  表格最多三列：**已完成 / 下一步 / 风险点**；各列内容酌情写，无则整列不写。
  每条用条目形式（"1. … 2. …"），短句关键词，禁止啰嗦。
  **负向约束（防呆）**：回复前先自检"本次是否改了代码/文件"——未改任何文件的任务
  （纯问答、查状态、讨论、纯验证），禁止使用表格，用正常回复。
