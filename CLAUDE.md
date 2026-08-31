# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 开发依据（先读这个）

`docs/product-handbook/` 是本产品唯一的开发蓝图（定位与原则 / 功能需求详规 / 交互与视觉规范 / 技术架构 / 路线图 / 知识库架构）。**接到任何开发任务，先读其 README.md 的「给 AI 助手的操作指引」节**，按其中的任务循环与硬性规则工作；本文件只负责"代码现状"，手册负责"该长成什么样"，两者冲突时以手册为方向、以本文件为现状细节。

## Status

**Backend implemented & fully tested (2026-08, 243 pytest + 8 docker-gated integration). Frontend is a Web SPA (React + Vite, served by the backend at `/`) — all milestones M2–M4 done & verified: connections/schema tree/results table, M3 AI chat rail (SSE) + report mode, M4 audit page / settings drawer / knowledge graph; M5 packaging/polish is the remaining loose end.** Product & architecture decisions below were locked during brainstorming and the backend implements them. Verified 2026-08 against the actual source.

## Product

AI-first database query tool (self-hosting Web) for developers. Hero feature: natural-language → SQL, backed by a **local, model-independent safety gate**. Positioned against DBeaver/DataGrip/Navicat by the trust story ("the AI can write, but it can never write dangerously"). Enterprise data privacy is the moat.

## Commands (backend)

```bash
# 一键启动（推荐）：自动 venv + 依赖 + 前端构建 + 起服务 + 开浏览器，幂等秒起
python3 backend/tabletalk.py

cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python scripts/seed_demo_db.py     # 生成 ~/.tabletalk/demo.db 演示库
.venv/bin/python -m uvicorn app.main:app --reload --port 8777   # 启动 sidecar
.venv/bin/python -m pytest -q                # 全量测试（243 个，SQLite）
docker compose -f docker-compose.integration.yml up -d && TABLETALK_INTEGRATION=1 .venv/bin/python -m pytest tests/integration -v   # PG/MySQL 集成测试（Docker 门控）
```

### Frontend (React SPA in `frontend/`, served by the backend)

```bash
cd frontend
npm install            # 首次
npm run dev            # Vite dev server（5173），proxy /api → 127.0.0.1:8777（需后端已起）
npm run typecheck      # tsc --noEmit
npm run build          # 产出 frontend/dist/，由后端 StaticFiles 同源托管
npm run screenshot     # Playwright 截图验证（自起后端 → 连演示库 → 知识审查 → AI 标签）
```

- 浏览器经 `GET /api/v1/bootstrap`（免鉴权）拿 `{token, dataDir}`，`src/renderer/src/hooks/useBootstrap.ts` 轮询注入 `src/renderer/src/api/client.ts`，请求自动带 `X-TableTalk-Token`。
- **生产访问**：起后端（`uvicorn app.main:app --port 8777`）+ `npm run build`，浏览器打开 `http://127.0.0.1:8777`。
- **开发访问**：后端单独起，再 `npm run dev` 后开 `http://localhost:5173`。
- 演示库由后端 lifespan 首次启动自动播种 `data_dir/demo.db`；连接页"使用演示库"即指向它。

Config via env / `.env` (see `backend/.env.example`): `TABLETALK_AI_PROVIDER` (mock|cloud|local), `TABLETALK_AI_BASE_URL/API_KEY/MODEL`, `TABLETALK_PORT`, `TABLETALK_DATA_DIR`, `TABLETALK_SIDECAR_TOKEN` (鉴权 token；缺省时启动生成并写入 `data_dir/tabletalk.token`，浏览器经 `/bootstrap` 读取), `TABLETALK_WEB_DIST` (SPA 构建目录，默认 `../frontend/dist`)。Runtime overridable via `PUT /api/v1/settings`.

## Tech stack (decided)

- **Frontend (built)**: React 18 + TypeScript + Vite + Zustand, a Web SPA served by the backend over same-origin HTTP (dev: Vite 5173 proxy)
- **Backend (built)**: local FastAPI (Python) sidecar in `backend/` — owns *all* DB connections, the safety gate, and AI orchestration
- **Databases**: SQLite (built-in demo, fully tested) + PostgreSQL (`psycopg`) + MySQL (`aiomysql`) — dialect adapters registered in `app/core/dialects/registry.py`; no local PG/MySQL server here, integration tests are Docker-gated
- **SQL parsing**: `sqlglot` for the safety gate's syntax tree

The sidecar architecture is deliberate: it reuses React + FastAPI skills, makes the safety gate independently pytest-testable, and the same service can later evolve into a team/private gateway server (audit, shared AI proxy).

## Backend architecture (implemented)

```
backend/app/
  main.py            FastAPI 入口 + CORS + sidecar token 鉴权 + 方言导入（注册）
  state.py           AppState singleton container (env, connections, pools, audit, knowledge, runtime)
  config.py          env-level Settings; reset_env() for tests; get_token() 鉴权 token
  api/               thin routers: health, connections, schema, query, audit, settings, ai, knowledge
  core/
    connections.py   ConnectionConfig registry (JSON persistence) — unified `dialect` field
    settings.py      RuntimeSettings (AI gateway + gate params), JSON persistence, PUT /settings
    pool.py          PoolManager: per-connection 小池（SQLite=1 串行，PG/MySQL 默认 3，TABLETALK_POOL_SIZE 可配）; run(fn) with auto-reconnect
    schema.py        dialect-aware schema discovery + summarize() + DDL export + preview; 30s cache
    query.py         execute / serialize / row cap (SQL 层自动注入 LIMIT) / limit-offset (sqlglot) / count_total / cancel
    dialects/        DialectAdapter ABC + registry + sqlite/postgres/mysql (self-register on import)
  safety/            ★ the safety gate — pure, model-independent, fully unit-tested
  ai/
    gateway.py       OpenAI-compatible LLMGateway (cloud/local) + MockProvider (no key needed)
    context.py       system prompt + schema summary + 领域标签路由 (structure only, never row data)
    preflight.py     统一意图层：关键词快判 + 单次 LLM {intent,tags} + 超时降级（6 意图）
    loop.py          function-calling chat loop, SSE events (think/sql_card/text/done)
    cost_tracker.py  每次 AI 调用记录 token/成本，存 SQLite cost.db
    agent/dispatcher.py  intent→skill 映射 + enabled 感知 + 降级
    tools/           11 个原子工具（register_tool 注册）
      sql.py         run_query / run_dml / draft_ddl（过安全闸门）
      schema_tools.py get_schema（合并旧 describe_table，可选 table 参数）
      query_audit.py 审计日志查询（时间/verdict/连接过滤 + recent 模式）
      ai_review.py   SQL 语义安全审查（advisory only，不具放行权）
      kb_read.py     知识库查询（文档/标签）
      kb_write.py    知识库维护（draft→confirm 流程）
      graph_read.py  图谱查询（表间关联，N 跳 BFS）
      graph_write.py 图谱维护（增/删边）
      manage_task.py 定时任务 CRUD
      suggest_followup.py 追问建议生成（loop 自动调用）
    skills/          8 个内置技能（skill = 意图 = 场景指导书）
      builtin/       query / write / ddl / report / knowledge / scheduler / refusal / general
    knowledge/       知识库新模块：store.py（SQLite docs+tags）+ route
    graph/           图谱新模块：store.py（SQLite edges + BFS 查询）
    tasks/           定时任务：storage.py + scheduler（asyncio）
  audit/logger.py    SQLite audit_log（兼容旧 JSONL 迁移）
```

### Safety gate — three tiers by origin

One gate applies to **both** AI-generated and manually-executed SQL. `POST /api/v1/query` flow: parse (sqlglot) → classify → rules → verdict ALLOW/REVIEW/BLOCK; REVIEW returns `preview_rows` (COUNT with same WHERE) and needs `confirm: true` to execute; BLOCK never executes. `app/safety/` is pure logic, fully covered by `tests/safety/`.

| Tier | Origin | Rules |
|------|--------|-------|
| Read (SELECT/SHOW/EXPLAIN/PRAGMA) | AI + manual | Runs directly, marked read-only |
| DML (INSERT/UPDATE/DELETE) | AI + manual | Gate required: no-WHERE blocked; affected-row preview; explicit confirm |
| DDL (CREATE/ALTER/DROP/TRUNCATE) | **Manual only** | Strong red-card confirm; AI gets BLOCK as defense-in-depth |

Key invariants:
- Gate is a **local rules engine, model-independent** — still blocks offline. Parse failures default to "write" (REVIEW), never ALLOW.
- Every executed statement logged to audit (statement, verdict, timestamp).
- DDL hard boundary: the AI's function-calling toolset has **no DDL execution tool** — only `draft_ddl` (generates script, sends to editor, never executes).

### AI toolset — 11 tools, no DDL execution

`get_schema` · `run_query` (read-only, gated, returns columns+rowcount only) · `run_dml` (gated, preview+confirm, never auto-executes) · `draft_ddl` (draft only) · `query_audit` (审计日志查询) · `ai_review` (SQL 语义安全审查, advisory only) · `kb_read` / `kb_write` (知识库查询/维护) · `graph_read` / `graph_write` (图谱查询/维护) · `manage_task` (定时任务 CRUD) · `suggest_followup` (追问建议, loop 自动调用)

8 skills (intent→skill 1:1): `query` (地板常开) · `write` (DML) · `ddl` (仅草稿，无执行工具) · `report` · `knowledge` · `scheduler` · `refusal` (地板常开) · `general` (只读兜底).

### Privacy red line

- **Sent to model**: schema context (tables/columns/types/FKs/comments) + current connection + selected table + conversation history + knowledge-base docs.
- **NOT sent by default**: raw row data. `run_query` tool returns columns + rowcount only; `include_data: true` (request-level opt-in) sends ≤N rows.
- **AI gateway configurable**: cloud API or any OpenAI-compatible local endpoint (Ollama/vLLM/private), or `mock` provider for keyless dev.

## Verified realities (what the code actually does — read before changing behavior)

These were confirmed by reading `app/` source in 2026-08 and differ from plausible assumptions:

- **`TABLETALK_GATE_REVIEW_THRESHOLD` and `gate_rules` are INERT.** They are configurable, persisted to `data_dir/settings.json`, and returned by `GET /settings`, but **no gate code reads them**. DML is always classified `REVIEW` regardless of estimated affected-row count; there is no row-count auto-allow. Don't build features assuming the threshold gates anything.
- **Read-only connections hard-BLOCK writes.** `api/query.py` blocks any non-ALLOW verdict (REVIEW included) when `ConnectionConfig.read_only` is set — stricter than the gate alone. The demo DB connection is read-only.
- **`cloud` provider with no `api_key` silently degrades to `mock`.** `gateway.is_effective_mock` treats `cloud`+missing-key as mock, so a misconfigured cloud URL "works" via deterministic mock and hides the error. `POST /ai/test` deliberately disables this fallback to surface real connectivity issues.
- **`SELECT` without `LIMIT` gets `LIMIT (max_rows+1)` injected at the SQL layer** (`query._auto_cap`, `TABLETALK_QUERY_MAX_ROWS` default 1000) so the *database* does less work, not just the transfer. `truncated` is inferred from the +1 row.
- **Large integers stringified to JSON** (`serialize_value`, `JS_SAFE_INT_MAX`) to avoid JS `Number` precision loss — relevant to the `big_values` seed table.
- **Knowledge base builds lazily once per process** and reloads from `knowledge-{conn_id}.db` (SQLite, `storage.SqliteStorage`, `TABLETALK_KB_STORAGE=json` 回退旧 JSON) artifact on restart. Schema discovery has a 30s cache (`core/schema.py`); structural changes need `?refresh=true` or cache invalidation.
- **Tag routing uses only *confirmed* tags** (`store.route_tables`); a freshly AI-proposed draft tag does not affect routing until a human confirms it.
- **Report mode forces `include_data=true`** (aggregated `GROUP BY` rows only) so the model can write numerically-traceable narration; chat mode defaults to `include_data=false` (columns+rowcount only). Both honor the "structure, not raw rows" red line.
- **Chat sessions ≠ audit.** AI `run_query` tool calls do *not* write audit (only manual `POST /query` and report queries do). Chat history lives in `data_dir/chat.db`; clicking "run SQL" writes JSONL to `data_dir/audit.log`. Viewing a session does not bump `updated_at`.
- **Multi-statement guard:** a batch with >1 statement where *any* is non-read is `BLOCK`. Pure multi-`SELECT` is allowed. (`_auto_cap`/`_apply_page` only rewrite single-`SELECT` parses.)
- **Auth exempt set is exactly two paths** — `GET /health`, `GET /bootstrap` — plus `OPTIONS` and anything outside `/api/*`. Token is a single shared secret in `data_dir/tabletalk.token` (chmod 600); `/bootstrap` serves it to the browser and is LAN-accessible by default (`bind 127.0.0.1` only — networked deploy needs a gate). CORS is wide-open (`allow_origins=["*"]` + `allow_credentials=True`).
- **Mock is the default provider** → the entire pipeline runs offline with deterministic responses out of the box.


## API surface (all under /api/v1)

`GET /health` · `CRUD /connections` + `/{id}/test` · `GET /connections/{id}/schema[/{table}[/preview|/ddl]]` · `POST /query` + `/query/cancel` + `/sql/format` · `POST /ai/chat` (SSE) + `/ai/selection` · `GET /audit` · `GET/PUT /settings` · `GET/POST/PUT/DELETE /tasks` + `/{id}/run` · `GET /cost/summary` + `/cost/daily` · `POST /suggestions/initial`

**Auth**: 中间件仅守 `/api/*`；除 `/health`、`/bootstrap` 与 OPTIONS 预检外，所有 `/api/*` 请求需带 `X-TableTalk-Token` 头（token 见 `config.get_token()`）；静态资源 / SPA 免鉴权。`/bootstrap` 向浏览器发放 token，仅本机可访问（默认绑 127.0.0.1），未来网络化部署需加门禁。

## Extending

- **New database**: implement `DialectAdapter` in `app/core/dialects/`, register via `@register_dialect`, add import to `app/core/dialects/__init__.py`. No other code changes.
- **Knowledge base semantic embedding**: retrieve 已是关键词+向量+图谱混合检索；真语义嵌入用 `TABLETALK_EMBEDDING_PROVIDER=api` + `TABLETALK_EMBEDDING_BASE_URL/MODEL/API_KEY`（任意 OpenAI 兼容 /embeddings，如本地 bge-m3），实现 `Embedder` 接口注入即可。
- **Adding a safety rule**: add a `RuleResult` in `app/safety/rules.py` + a case in `tests/safety/`.
