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
- **gate_rules 生效**：`gate_rules`（规则覆盖，只许收严）经 `assess_configured` 真实参与判定；
  规则目录/元数据见 `safety/rules.py` 的 `RULE_META`，配置 UI 在前端审计页 RulesDrawer。
- **成本阈值 = `policy.threshold`**（默认 100000）：只读 ALLOW 查询预计扫描超阈值 → 升 REVIEW
  （`api/query.py` A5）；预估失败放行并审计。`gate_review_threshold` 已删除（曾被 policy 遮蔽的死配置）。
- Read-only connection (`read_only=True`) hard-BLOCKS any non-ALLOW verdict in `api/query.py`.

## AI (`app/ai/`)

- 5 tools: `get_schema`, `describe_table`, `run_query` (read), `run_dml` (write),
  `draft_ddl` (**draft only, never executes**). No DDL exec tool by design.
- `gateway.is_effective_mock`: `provider == "mock"` (test-only built-in simulator).
  `POST /ai/test` passes through to real provider when not mock.
- **引擎合一（2026-09-08）——唯一 ReAct 循环（`app/ai/harness.py`）**：chat_stream、
  意图层（decompose/intent.py）、skill 路由层（skills/、api/skills.py、dispatcher）已删除。
  循环核心 `run_react_loop` + 两个入口：
  - `harness_stream`（默认对话流，`loop.stream()` 分发）：readonly 工具全集常开
    （trust 元数据过滤，单一事实来源），**mutating 工具不在场**（先审后动）。两个交互工具：
    `ask_user`（结构化澄清，结束回合等回答，clarify 事件带 `origin: ask_user`）、
    `propose_plan`（写操作/编排提议 → chat.db `pending_plans` → `plan_pending` 事件 → 人审；
    evidence_summary ≥30 字强制，防"没查就提计划"）。探索预算 MAX_TURNS=30，墙钟 600s。
  - `controlled_step_stream`（受控计划步骤）：步骤指令注入（req._task_target）+
    工具面按动作查表（`_STEP_TOOLS`：write 步骤带 run_dml + DML confirm_token 流内建核心）、
    非交互、轮数预算 6。
  DML REVIEW→confirm_token 流、出网清单（manifest 事件）、覆盖率指标都在循环核心内。
- **`loop.py` 统一入口 `stream()`**：服务端历史压缩 → preflight（准备层）→ 问题库快路径
  （原文命中 → 零模型确认卡，从旧 chat_stream 上提）→ harness。
  `task_runner`：modality=report → report_stream，其余 → controlled_step_stream。
- **报告模式（`report.py`）**：纯管线非循环（澄清→规划→逐章执行→成文，三次单发 LLM +
  直接执行）；强制 `include_data=true` for aggregates only。
- **受控计划执行（`app/api/plans.py`）**：`POST /ai/plans/{id}/confirm|reject`。
  confirm 分段执行（executor 外层 + task_runner → controlled_step_stream）：遇到 run_dml review 卡
  → 挂起（`plan_awaiting`，剩余步骤+进度写回 pending_plans）→ DML 走既有 confirm token 流；
  空步骤 confirm = 收尾（`plan_done`）。done_count 列跨段累计。
  前端：`plan_pending` → 计划确认卡（AiRail），确认/拒绝/续段按钮。
- **历史统一（committed-turn）**：服务端 chat.db 是唯一历史来源——默认流/显式 mode/报告模式
  共用（stream() 入口加载 + compress_hist 压缩 + 骨架化回喂 `_server_history_for_model`；
  前端 localStorage 仅为展示缓存）。循环在每轮边界发内部事件 `_commit`（不下发前端），
  ai.py gen() 逐轮落库 + sql_card 工件即时落 artifacts；**ai.py 流末一次性持久化已删除**。
- **provider 重试回灌**：run_react_loop 的 LLM 流式调用失败自动重试 ×2（指数退避 2s/4s），
  重试前注入 `[turn_retried]` user 消息；重试安全由 committed-turn 保证。
- **断连 carry-over**：客户端断开/超时（GeneratorExit）→ `core_query.cancel` 取消在飞查询 +
  `chat_ws/<sid>/state.json` 记 last_interrupt；下次请求注入续答摘要（用后即清）。
  消息序列合规：tool 消息前必有 assistant tool_calls（严格 provider 兼容）。
- Default provider = unconfigured → AI unavailable. `mock` = test-only built-in simulator (needs explicit selection).
- **任务创作Agent（POST /tasks/agent，harness 式 ReAct）**：工具全程常开
  （get_schema + candidate_write/read/test，候选工作区在 `data_dir/task_ws/<session_id>/`）。
  needs 三态：`plan`（---PLAN--- 需求确认单，questions 非空禁止产脚本）/ `proposal`
  （---PROPOSAL---）/ `clarify`。候选脚本必须通过沙箱自测（candidate_test）才允许交付——
  未测交付被硬拦截 3 次后放行但标记 `untested`；交付 script 与通过的候选快照不一致时
  后端强制以快照为准。消息序列合规：assistant tool_calls 消息先于 tool 结果回灌。
  连接 id/name 双向归一（`_resolve_connection`），解析失败报错不静默猜。
- **部署门禁（POST /tasks/deploy）**：py_compile 语法校验 + 双重沙箱实测
  （TABLETALK_TEST=1，写 dry-run / 读真跑）失败拒绝落盘；新任务默认 `# enabled: false`
  （覆盖已有任务保留原状态）。用户需"立即运行"确认输出后手动启用。
- **沙箱网络白名单**：`TABLETALK_SANDBOX_PORTS` 环境变量（默认8777,5432,3306）。

## Core (`app/core/`)

- `connections.py`: creds stored in **system SQLite** `data_dir/tabletalk.db`（`connections` 表，`data` 列存 JSON；
  旧 `connections.json` 明文仅作一次性迁移源，2026-09 起实际存储为 system_db）
  (`credential_ref` reserved for future keychain). `public()` masks password.
- `pool.py`: SQLite pool size 1 (serialized); PG/MySQL = `TABLETALK_POOL_SIZE` (default 3);
  auto-reconnect once on error.
- `query.py`: `SELECT` w/o LIMIT gets `LIMIT (max_rows+1)` injected at SQL layer
  (`TABLETALK_QUERY_MAX_ROWS` default 1000); big ints stringified for JSON (JS precision).
- `schema.py`: 30s cache; pass `?refresh=true` after structural changes.
- Dialects: `sqlite`/`postgres`/`mysql` via `DialectAdapter` registry; add by
  implementing adapter + importing it in `app/core/dialects/__init__.py`.

## Knowledge (`app/knowledge/`)

- Lazy build once per process; artifact `data_dir/knowledge-{conn_id}.db` (SQLite, `storage.SqliteStorage`, `TABLETALK_KB_STORAGE=json` 回退) reloaded on restart;旧 `knowledge-{conn_id}.json` 自动迁移。
- **连接级互斥（D1）**：build/sync/confirm/discard 经 `BuildJobManager.begin_op/end_op` 占坑互斥
  （sync 手动 + SyncLoop.tick 均注册；冲突 409）。失败路径与取消对称：`clear()` + `kb_status=none`；
  启动迁移把持久化 `building` 归位 none。tags+graph AI 双失败 → 构建判失败（stats["degraded_phases"]，
  不进 pending_review）。
- **增量 rename 检测**：removed↔added 列指纹一致 → 知识继承（表知识 TK.name 同步改写/样本/向量/
  标签绑定/图边/注释缓存/baseline 改挂新表名），不重新注释；歧义多候选保守回退删+建。
  逻辑在 `BuildService.apply_renames/adjust_diff_for_renames`，全量 diff 模式与增量共用。
- **注释缓存**（2026-09）：`annotate_table` 按 `sha1(DDL+规范化样本+PROMPT_VERSION+model)` 缓存 items
  （随快照落盘，`KbSnapshot.annotation_cache`）；同输入零 LLM 调用、提案措辞不漂。
  prompt 模板变更须 bump `annotator.PROMPT_VERSION`。
- **构建档位**：`build(annotate_mode, tag_mode)`——`annotate_mode=diff|full`（diff 只提案变化表，
  首建等价 full）；`tag_mode=keep|anchor|fresh`（keep/anchor 注入既有 confirmed 域锚点，
  anchor 允许 AI 提议 renamed_from/merged_from 走审核批准；fresh 清标签库从零划分）。
  API `POST /build` 透传 `annotate_mode/tag_mode`。
- **sync 任务化**（2026-09）：`POST /sync` 快速检查后启动 `kind="sync"` job（与 build 同一
  progress/SSE 通道）；成功且有提案 → `pending_review`（此前 sync 提案永远停在 ready、审核入口
  隐身的缺口已修）；无提案 → ready；失败 → 保持 ready。抽样只抽 touched 表
  （`_touched_for_sync`），手动与 SyncLoop 状态流转共用 `apply_sync_result_status`。
- **限流（D4）**：构建期四 AI 阶段共享一个 `_AdaptiveLimiter`（阶段一整任务占坑，阶段二~四按次占坑）。
- 构建热路径落盘走 `_save_conn_async`（to_thread），交互写仍走同步 `_save_conn`。
- **round 对比区已落盘**（`meta round_json`）：重启后审核镜头 baseline/diff 不丢。
- **审核收尾唯一路径**：`batch-review` + `review/finalize`；`/confirm-all` 仅测试兼容保留，
  前端入口已退役（审核镜头底部裁决栏是唯一收尾点，裁决全暂存）。
- **单表重注**：`POST /{conn_id}/annotate-table`（重试失败表/手动刷新单表；与构建共用缓存）。
  注释失败的表挂 `round.failed_tables`，overview.round 透出，审核镜头可重试。
- **增量补齐**：`incremental_build` 会对 touched 表补跑 `annotate_filters`/`annotate_constants`/
  `ingest_values_candidates`（后者读取链 = confirmed values → proposed_values）。
- 逐表注释 prompt 的 `db_tables` 上下文大库裁剪为 FK 邻表+同前缀（≤30，`_related_tables`）。
- **图谱候选注入**（2026-09）：阶段三 prompt 注入 `build_draft_edges`（FK+命名）候选块（`_deterministic_candidates_block`，cap 120），引导 LLM 聚焦候选之外的发现；权威解析与确定性通道不变。
- **进度流式**（2026-09）：阶段一空跑（无 touched 表）直接 100%+"跳过"帧；`_chat_with_beat` 支持 `stream=True` 消费 `chat_stream` + `_count_json_items` 容忍式计数（detail 实时"已识别 N 条关系"），percent 走 `_climb_pct` 两段式爬坡（前 20s 半窗 + climb_seconds 匀速，恒 ≤ p_to-1）；阶段二/三已接流式，读超时=卡顿上限而非总时长。
- SyncLoop：`kb_sync_minutes` = 连接级最小间隔（30s tick 节拍不变）；该键**已进** settings
  update() 白名单（=0 关闭自动同步）。
- `route_tables` uses **confirmed tags only** — draft tags don't affect routing until confirmed.
- Embedder = OpenAI-compatible `/embeddings` endpoint only (embedding provider/base_url/api_key/model); no offline fallback.

## Data dir (`TABLETALK_DATA_DIR`, default `~/.tabletalk`)

`tabletalk.db`（system SQLite：connections/settings/auth/approvals/questions/skills/codify） · `settings.json` · `tabletalk.token` (chmod 600) · `audit.log` (JSONL) ·
`chat.db` (SQLite sessions) · `knowledge-{conn_id}.db` (SQLite, 旧 `knowledge-{conn_id}.json`/`knowledge.json` 自动迁移) · `demo.db`.

## Tests

`pytest.ini` sets `asyncio_mode=auto` (no decorator on `async def` tests).
Full suite SQLite-only: `cd backend && .venv/bin/python -m pytest -q`.
Integration (`tests/integration`, PG/MySQL) Docker-gated: `TABLETALK_INTEGRATION=1 .venv/bin/python -m pytest tests/integration -v`.
