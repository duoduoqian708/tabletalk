# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 开发依据（先读这个）

`docs/product-handbook/` 是本产品唯一的开发蓝图（定位与原则 / 功能需求详规 / 交互与视觉规范 / 技术架构 / 路线图 / 知识库架构）。**接到任何开发任务，先读其 README.md 的「给 AI 助手的操作指引」节**，按其中的任务循环与硬性规则工作；本文件只负责"代码现状"，手册负责"该长成什么样"，两者冲突时以手册为方向、以本文件为现状细节。

## 测试纪律（最高优先级，用户亲自测试）

不要在自动化/浏览器验证脚本上投入过多资源。改动完成后做到：类型检查 / 构建通过 + 核心接口或核心逻辑的**一次性冒烟验证**即可。边缘业务路径、重复场景、复杂交互脚本一律不做——**用户会亲自测试验证**。不要为了凑全绿结果反复调试测试脚本；脚本本身若不稳定，直接放弃脚本化验证，改为向用户说明已验证的范围与验证方式。

**测试套件定位（2026-09 精简，154 个）= 主链路 + 安全不变式，仅此两类**：安全闸门（`tests/safety/`、`tests/test_blast.py`、`tests/test_gate_attack.py`）与 AI 对话主链路（`tests/ai/` chat/confirm/压缩/问题库/技能白名单 + `tests/api/test_api.py` E2E）。知识库内部逻辑、增量同步、图谱构建、审计聚合、core 工具层、tasks 等**不写测试**，靠用户走查。**测试失败时先归因**：主链路/安全坏了 → 改代码；细节断言过时 → 直接删该断言或整个过时用例，不补场景、不修 fixture。项目演进期一改就要跟着维护的用例是负债；删错了将来遇到再按上面规则处理。

## Status

**Backend implemented & tested (2026-09 精简套件, 154 pytest + 8 docker-gated integration). Frontend is a Web SPA (React + Vite, served by the backend at `/`) — all milestones M2–M4 done & verified: connections/schema tree/results table, M3 AI chat rail (SSE) + report mode, M4 audit page / settings drawer / knowledge graph; M5 packaging/polish is the remaining loose end.** Product & architecture decisions below were locked during brainstorming and the backend implements them. Verified 2026-08 against the actual source.

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
.venv/bin/python -m pytest -q                # 全量测试（154 个：安全闸门 + AI 主链路，SQLite）
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

Config via env / `.env` (see `backend/.env.example`): `TABLETALK_AI_PROVIDER` (supplier name e.g. deepseek/glm/openai, or mock for testing; leave empty = AI unavailable), `TABLETALK_AI_BASE_URL/API_KEY/MODEL`, `TABLETALK_PORT`, `TABLETALK_DATA_DIR`, `TABLETALK_SIDECAR_TOKEN` (鉴权 token；缺省时启动生成并写入 `data_dir/tabletalk.token`，浏览器经 `/bootstrap` 读取), `TABLETALK_WEB_DIST` (SPA 构建目录，默认 `../frontend/dist`)。Runtime overridable via `PUT /api/v1/settings`.

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
  ai/                ★ 引擎合一（2026-09）：唯一 ReAct 循环，无意图层/skill 路由层
    harness.py       循环核心 run_react_loop + 两个入口：harness_stream（默认对话流：
                     readonly 工具常开 + ask_user 澄清 + propose_plan 受控流入口）与
                     controlled_step_stream（受控计划步骤：指令注入 + 按动作收窄工具集）
    loop.py          统一入口 stream()：服务端历史压缩 → preflight → 问题库快路径（零模型
                     确认卡）→ harness；task_runner（受控步骤路由）；历史骨架/断连续答助手
    gateway.py       OpenAI-compatible LLMGateway + providers/ 供应商适配层 + MockProvider
    context.py       上下文组装：schema 摘要 + 图路径串 + 表级过滤器 + 概念字典 + few-shot
    preflight.py     准备层（不做意图分类）：脱敏/出网清单/tags/追问检测/skip_retrieval
    report.py        报告管线（非循环：澄清→规划→逐章执行→成文，三次单发 LLM + 直接执行）
    executor.py      execute_plan 外层 for（只服务受控计划与报告模式：task_* 事件 + 失败即停）
    cost_tracker.py  每次 AI 调用记录 token/成本，存 SQLite cost.db
    tools/           原子工具（register_tool 注册，trust 元数据为注册强制项）
      sql.py         run_query / run_dml / draft_ddl（过安全闸门）
      schema_tools.py get_schema（合并旧 describe_table，可选 table 参数）
      query_audit.py 审计日志查询（时间/verdict/连接过滤 + recent 模式）
      ai_review.py   SQL 语义安全审查（advisory only，不具放行权）
      kb_read.py     知识库查询（文档/标签/概念）
      kb_write.py    知识库维护（draft→confirm 流程）
      graph_read.py  图谱查询（表间关联，N 跳 BFS）
      graph_write.py 图谱维护（增/删边）
      interaction.py ask_user（结构化澄清）/ propose_plan（受控流入口，evidence 强制）
      task_dev.py    任务创作 Agent 专用（candidate_write/read/test 沙箱工具）
      suggest_followup.py 追问建议生成（loop 自动调用）
    knowledge/       知识库模块（facade + 语义/图/行为层）
    graph/           图谱模块（SQLite edges + BFS 查询）
    tasks/           脚本化定时任务（全脚本化）：cron.py(手写5字段) + jobs.py(声明头注册表) +
                     runner.py(子进程+SDK过闸) + scheduler.py(beat循环) + store.py(jobs.db runs) +
                     seed.py(播种lib.py SDK+系统保留脚本) + migrate.py(旧tasks.db迁移)
  api/system.py      平台系统端点：日志保留清理（脚本经 SDK retain_logs 调用，审计留痕）
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

### AI toolset — 两张工具面，no DDL execution（引擎合一后无 skill 层）

- **harness 对话面**（trust=readonly 元数据过滤，单一事实来源）: `get_schema` · `run_query` (read-only, gated, returns columns+rowcount only) · `draft_ddl` (draft only) · `query_audit` · `ai_review` (advisory only) · `kb_read` · `graph_read` · `ask_user` (结构化澄清) · `propose_plan` (受控流入口，evidence ≥30 字强制) · `suggest_followup`
- **受控步骤面**（propose_plan 确认后按步骤动作查表）: write 步骤 = `run_dml` (gated, preview+confirm, never auto-executes) + `run_query`/`get_schema`/`ai_review`；kb 步骤 = `kb_read`/`kb_write`/`graph_read`/`graph_write`；mutating 工具在默认对话流不可见——写操作一律走 propose_plan 先审后动

### Privacy red line

- **Sent to model**: schema context (tables/columns/types/FKs/comments) + current connection + selected table + conversation history + knowledge-base docs.
- **NOT sent by default**: raw row data. `run_query` tool returns columns + rowcount only; `include_data: true` (request-level opt-in) sends ≤N rows.
- **AI gateway configurable**: any OpenAI-compatible endpoint (cloud API, Ollama/vLLM/private), or `mock` provider for testing. Unconfigured = AI unavailable.

## Verified realities (what the code actually does — read before changing behavior)

These were confirmed by reading `app/` source in 2026-08 and differ from plausible assumptions:

- **`gate_rules` and `policy.threshold` are LIVE (wired via `assess_configured`).** Rule overrides only tighten (strictness ladder, `RULE_META` floor rules); cost threshold escalates read queries whose estimated scan exceeds `policy.threshold` to `REVIEW` (`api/query.py`). DML is always classified `REVIEW` regardless of estimated affected-row count; there is no row-count auto-allow. `gate_review_threshold` was removed (dead config, permanently shadowed by `policy.threshold`).
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
- **定时任务已全脚本化（2026-09）**：任务 = `data_dir/jobs/*.py`，声明头（`# name:`+`# cron:` 必填，`# connection`/`# enabled`/`# system` 可选）驱动注册；无声明的文件是辅助模块（`lib.py` SDK）。调度器 beat 循环每 30s 扫目录、到点**子进程**跑脚本（`lib.py` 经 sidecar API → 闸门 → 审计），删脚本即任务消失。`Origin.SCHEDULED` 仅放行 INSERT（scheduled-insert 规则），UPDATE/DELETE 无 WHERE、DDL、只读连接一律拦。脚本查询要求连接 KB 已 ready（`POST /query` 的既有硬约束）。旧 `tasks.db`（sql/natural_query）首启迁移为脚本；`manage_task` 工具下线，定时需求由任务页 AI 对话承接。系统保留脚本（`日志保留清理`）调用 `POST /system/retain` 按天清三库并留痕，保留天数 = 改脚本顶部常量。旧 `app/ai/tasks/` 已删。

- **任务创作Agent使用状态机模式（2026-09）**：任务创作会话（`POST /tasks/agent`）使用显式状态机管理Agent生命周期。状态定义在 `app/ai/tools/task_states.py`，包含5个状态：INIT（初始化）、GENERATING（生成脚本）、TESTING（测试脚本）、DELIVERING（交付提案）、CLARIFYING（澄清问题）。每个状态有明确的合法工具列表，状态转换记录日志便于调试。状态机确保Agent在每个状态下只能执行合法操作，修复了原有隐式状态逻辑的缺陷。

- **引擎合一完成（2026-09-08）**：默认对话流 = harness 单循环（`harness.py: run_react_loop`），chat_stream/意图层（decompose/intent.py）/skill 路由层（skills/、api/skills.py、dispatcher）已删除；受控计划步骤经 `controlled_step_stream`（工具面按 `_STEP_TOOLS` 查表，write 步骤带 run_dml + DML confirm_token 流内建于循环核心）。问题库快路径从循环上提到 `loop.stream()`（此前只在旧 chat_stream 里，harness 流是功能回归）。报告流是纯管线非循环（澄清/规划/成文三次单发 LLM + 直接执行）。事件协议一套：`turn_start/stage×2/manifest/scene_start/subtask_*/think/text/sql_card/block/clarify(origin)/plan_pending/_commit/scene_done/done`，`task_*/section/narration` 仅报告/受控计划流。
- **前端契约（同轮修复）**：sql_card 事件到达即挂卡（旧 attachCards 依赖永不触发的 stage sql/gate 事件，导致 needs_confirm 卡在 UI 上永不出现）；clarify 事件带 `origin`（ask_user 的回答续 harness 默认流，report 的回答才走 mode=report 重传 replay）；STEP_DEFS 四步静态模板与 ThinkPanel 已删除（渲染优先级 tasks > subtasks）；harness 流补发 manifest（出网清单 UI 恢复）。


## API surface (all under /api/v1)

`GET /health` · `CRUD /connections` + `/{id}/test` · `GET /connections/{id}/schema[/{table}[/preview|/ddl]]` · `POST /query` + `/query/cancel` + `/sql/format` · `POST /ai/chat` (SSE) + `/ai/selection` · `GET /audit` · `GET/PUT /settings` · **任务（全脚本化）**`GET /tasks` / `POST /tasks/deploy` / `POST /tasks/agent`(AI 创作对话) / `POST /tasks/cron/preview` / `GET /tasks/{name}/runs` / `GET|PUT /tasks/{name}/script` / `PUT|DELETE /tasks/{name}` / `POST /tasks/{name}/run` · `POST /system/retain`(日志保留) · `GET /cost/summary` + `/cost/daily` · `POST /suggestions/initial`

**Auth**: 中间件仅守 `/api/*`；除 `/health`、`/bootstrap` 与 OPTIONS 预检外，所有 `/api/*` 请求需带 `X-TableTalk-Token` 头（token 见 `config.get_token()`）；静态资源 / SPA 免鉴权。`/bootstrap` 向浏览器发放 token，仅本机可访问（默认绑 127.0.0.1），未来网络化部署需加门禁。

## Extending

- **New database**: implement `DialectAdapter` in `app/core/dialects/`, register via `@register_dialect`, add import to `app/core/dialects/__init__.py`. No other code changes.
- **Knowledge base semantic embedding**: retrieve 已是关键词+向量+图谱混合检索；真语义嵌入用 `TABLETALK_EMBEDDING_PROVIDER=api` + `TABLETALK_EMBEDDING_BASE_URL/MODEL/API_KEY`（任意 OpenAI 兼容 /embeddings，如本地 bge-m3），实现 `Embedder` 接口注入即可。
- **Adding a safety rule**: add a `RuleResult` in `app/safety/rules.py` + a case in `tests/safety/`.
