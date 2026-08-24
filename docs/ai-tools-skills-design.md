# AI 工具与技能设计方案（v1）

> 日期：2026-08-23 · 状态：定稿

---

## 1. 设计原则

### 工具（Tool）

- **工具 = 原子能力**：一把工具，一个动词，一个对象，不做编排
- **每把工具自带信任等级**（readonly / mutating），安全执行点永远在工具层
- **未注册即禁止**——模型只能调用已注册的工具白名单

### 技能（Skill）

- **技能 = 意图 = 场景指导书**：不执行任何东西，只告诉模型「这个场景里按什么顺序调哪些工具、上下文怎么处理」
- **意图识别 1:1 映射技能**：意图识别完，后面走哪条路就定了
- **技能对工具只做交集过滤**（收窄原则）：任何技能组合跑不出工具信任的并集；一个能力"不能做"的最强保证是该工具根本不存在

### 跨意图操作规则

用户需求有时跨越多个技能领域（如"先查销售额再记到知识库"），1:1 映射下需要辅助工具解决：

- **意图识别路由到主要目标技能**（"记到知识库"→ knowledge）
- **该技能可引用辅助技能的只读工具**（knowledge 加入 `run_query`）
- **写操作不可跨技能**（knowledge 不能调 `run_dml`；query 不能调 `kb_write`）
- **安全边界**：辅助工具必须是 readonly，不突破铁律 3

这保证跨意图只读操作走通，写操作仍受各自技能白名单严格约束。

### 信任层级

工具层管信任（不可开关）→ 技能层管能力（用户勾选开关）→ 意图层管路由。

### 隐私与出网不变式（三条铁律）

贯穿全篇，违反任何一条都算实现错误：

1. **行数据不出网，工件进出模型上下文恒经脱敏管道**。`run_query` 返回 columns+rowcount（不是 raw rows）；`include_data` 是 request 级 opt-in（限 N 行）；本地落盘不算出网。`ai_review` 是模型调用——它把 SQL 发出去审，必须走单管道（脱敏→清单→审计），**它本身不产生行数据出网**（SQL 是结构，不是数据）。

2. **模型可见世界恒代号化**。schema、tool 入参回显、tool result 回喂模型前一律代号化（B3 往返不变式）。`get_schema` 结果回喂模型前走 `codify_schema`；tool result 里的表/列名回喂前走 `codify_columns_for_model`；展示层由 `decodify_text` 还原。`ai_review` 收到的 SQL 同样在代号化世界里。

3. **每次模型调用（含 ai_review）有清单有审计**。主 loop、report、**preflight、refusal、ai_review**——每一次 LLM 调用都有 manifest + egress 审计。清单口径不含例外。`ai_review` 的清单标记 `source=egress-review`。

4. **单管道约束**：`context → 脱敏 → 清单 → gateway`，禁止旁路直连模型。工具层不直接调 gateway，所有模型调用走 loop 统一入口。

---

## 2. 安全架构（两层，不占工具位）

| 层 | 名称 | 行为 | 是否可绕过 |
|---|---|---|---|
| 强制层 | 本地规则引擎 | 始终在线，拦截 SQL 级别风险（无 WHERE 的 DML、批量操作等） | 不可绕过，纯函数 |
| 增强层 | `ai_review` 工具 | 模型主动调用，获取语义级安全建议 | 可选，模型/框架按需触发 |

`ai_review` 是工具层的原子能力，不是技能；query/write 指导书会引导模型在复杂语义场景下调用它，但最终放行权始终在本地规则手里。

### 定时任务信任模型（v1 策略）

定时任务是**无人值守执行**——执行时刻没有人按确认按钮，写操作的预览+确认协议直接失效。因此：

**v1 硬性规定：定时任务只允许 SELECT 查询，禁止 DML/DDL。**

- `manage_task` 创建任务时，runner 执行前**强制校验 SQL 是否为只读**（过 `assess_sql`，verdict 必须为 `allow`）
- 非 SELECT 语句一律拦截，不执行、不预览、不静默降级
- 管理操作（创建/修改/删除任务定义）本身走 `manage_task` 的 mutating 信任 + 确认协议，不受此限制

**runner 执行流程**：
```
task 触发 → 加载任务 SQL → assess_sql → 
  verdict != allow → 拦截 + 审计日志（status=blocked_unsafe, reason=readonly_required）
  verdict == allow → 成本闸（EXPLAIN 估算超阈值 → cost_degraded 告警/拦截）
                    → 执行 → 审计日志（status=scheduled_exec, elapsed_ms=...）
```

**成本防护**：runner 走与 `run_query` 相同的 `assess_sql` 路径，自动继承 EXPLAIN 成本估算保护（`TABLETALK_GATE_COST_THRESHOLD` 超阈值升 REVIEW 或拦截）。**手动触发按钮（§7 控制台）走同一条 runner 校验链路，不成为旁路。**

**`natural_query` 字段**：保留为人读描述（方便用户在控制台看到"统计每日订单"），**不做延迟 SQL 生成**。原因：v1 不引入执行时 LLM 调用的复杂度（每次执行需额外 LLM 调用生成 SQL，增加成本和非确定性）。v2 可探索 `natural_query → SQL` 的延迟生成路径。

这把整类风险关掉，不需要复杂的无人授权流程。未来如需定时 DML，单独开 v2 信任模型。

---

## 3. Tool 总表（11 把）

| # | Tool | 动词+对象 | trust | confirm | 关键约束 |
|---|---|---|---|---|---|
| 1 | `run_query` | 查数据 | readonly | none | 自动注入 LIMIT、行数封顶；核心高频工具 |
| 2 | `run_dml` | 增删改数据 | mutating | card | 预览行数 + 影响面 + 回滚方案 → 确认卡（最强确认） |
| 3 | `get_schema` | 查表结构 | readonly | none | 30s 缓存；结构元数据，不过闸门；回喂模型前代号化 |
| 4 | `draft_ddl` | 改表结构 | readonly | none | 只出草稿，发编辑器，**永不执行** |
| 5 | `ai_review` | AI安全校验 | readonly | none | 只出意见，不具放行权；自身是模型调用，走单管道+清单+审计 |
| 6 | `query_audit` | 查审计日志 | readonly | none | 按时间 / verdict / 连接过滤；结果过脱敏管道 |
| 7 | `kb_read` | 查知识库 | readonly | none | 读文档、注释、领域标签 |
| 8 | `kb_write` | 维护知识库 | mutating | card | 新增 / 更新 / 确认 / 拒绝草稿（轻确认，亮出内容） |
| 9 | `graph_read` | 查图谱 | readonly | none | 读表间关联（FK + 值重叠边） |
| 10 | `graph_write` | 维护图谱 | mutating | card | 加 / 删除 / 更新边（轻确认，亮出变更） |
| 11 | `manage_task` | 管理定时任务 | mutating | card | 创建 / 查看 / 修改 / 删除 / 启停；**执行的 SQL 只允许 SELECT** |

**trust × confirm 矩阵**：
- `readonly + none`：直接执行，过闸门/脱敏管道（run_query, get_schema, draft_ddl, query_audit, kb_read, graph_read）
- `readonly + none`（模型调用）：`ai_review`——走单管道+清单+审计，本身不执行 SQL
- `mutating + card`：预览变更内容，用户点确认卡执行（run_dml, kb_write, graph_write, manage_task）
- `mutating + admin`：需管理员审批（v1 未使用，预留）

**永不注册（红线）**：删除数据源 · 修改模型配置 · 任意代码执行 · DDL 直接执行。

---

## 4. Skill 总表（6 个）

| # | Skill | 覆盖意图（用户语言） | 工具集 | 开关 |
|---|---|---|---|---|
| 1 | `query` | 查数据、查表结构、查审计日志、SQL 安全审查 | run_query, get_schema, query_audit, ai_review | **地板常开** |
| 2 | `write` | 增删改数据、改表结构 | run_dml, draft_ddl, run_query, get_schema, ai_review | 可关 |
| 3 | `report` | 出报告、导出结果 | run_query, get_schema | 可关 |
| 4 | `knowledge` | 知识图谱查询 + 维护 | kb_read, kb_write, graph_read, graph_write, get_schema, run_query | 可关 |
| 5 | `scheduler` | 定时任务管理 | manage_task, run_query, get_schema | 可关 |
| 6 | `refusal` | 拒答 / 闲聊 | （无） | **地板常开** |

**地板常开**：`query` + `refusal` 不可关闭（query 是核心功能；refusal 是兜底，"拿不准当 query"路由依赖它）。

**scheduler 技能的 run_query/get_schema 用途说明**：这两个工具仅用于**创建任务前**验证 SQL（检查表存在、试跑确认能通）。runner 实际执行时不经过 scheduler 技能——它直接调 run_query 执行存储的 SQL，不产生无人值守写操作的安全问题。

**关闭的技能给出降级出路**：路由到不可用技能时明确告知"此能力已关闭，可在设置开启"。

---

## 5. 意图识别与路由

```
用户问题
  │
  ├─ 关键词快判（多数请求零 LLM，直出意图）
  │   ├─ "查 / 统计 / 多少 / 哪些表 / 审计" → query
  │   ├─ "删掉 / 改成 / 加列 / 建表"       → write
  │   ├─ "报告 / 分析 / 导出"              → report
  │   ├─ "知识 / 注释 / 关联 / 图谱"       → knowledge
  │   ├─ "每天 / 定时 / 调度"              → scheduler
  │   └─ 平台外特征词                       → refusal
  │
  └─ 拿不准时：一次 LLM 判定（输入 = 问题脱敏后 + 对话尾部 + 已确认标签）
       └─ 仍拿不准 → 默认 query（误判代价最低，闸门兜底）
```

**兜底规则**：误判为其他数据面意图（query/write/report）的代价都是"便宜错误"（循环可纠正 + 闸门兜底）；**唯一贵的误判是 offtopic**（拒答了本该回答的问题），由"拿不准当 query"堵死。

---

## 6. 工具-技能矩阵（快速查表）

| | run_query | run_dml | get_schema | draft_ddl | ai_review | query_audit | kb_read | kb_write | graph_read | graph_write | manage_task |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **query** | ✅ | | ✅ | | ✅ | ✅ | | | | | |
| **write** | ✅ | ✅ | ✅ | ✅ | ✅ | | | | | | |
| **report** | ✅ | | ✅ | | | | | | | | |
| **knowledge** | ✅ | | ✅ | | | | ✅ | ✅ | ✅ | ✅ | |
| **scheduler** | ✅ | | ✅ | | | | | | | | ✅ |
| **refusal** | | | | | | | | | | | |

---

## 7. 平台 UI 模块（工作台，不占工具位）

### 定时任务调度控制台

独立 UI，功能：查看全部任务列表、手动触发、启用/禁用、删除、执行历史。AI 通过 `manage_task` 工具创建的任务同步显示在此。

### AI 使用成本仪表盘

纯 UI 展示层，读取现有审计日志数据（egress manifest / token 计数 / 调用记录）：各技能调用次数、token 消耗趋势、成本统计。是"敢让 AI 进内网"信任故事的关键一环——用户必须能看到 AI 做了什么、花了多少钱。

---

## 8. 待决事项

| # | 问题 | 影响 |
|---|---|---|
| Q1 | `manage_task` 的 CRUD 权限在 chat 里是否需要分级（创建自由，删除需确认） | 安全策略 |
| Q2 | 定时任务的执行结果是否写入审计日志（当前审计只覆盖直接执行） | 一致性 |
| Q3 | `ai_review` 工具的具体触发时机写入 query/write 指导书的具体措辞 | 实现阶段 |
| Q4 | 技能运行中发现需要白名单外写工具时的降级行为（如 query 技能中模型想调 kb_write） | 用户体验 |

**Q4 建议方案**：固定降级话术——"这需要{目标技能名}能力，你可以说'{引导句}'让我切换到对应场景"。让路由自然重走，不破坏技能白名单。例如：query 技能中模型发现需要 kb_write → 回复"这需要知识库维护能力，你可以说'把这条记到知识库'让我切换到知识场景"。

## 9. 实现前置依赖

| 依赖 | 说明 | 影响范围 |
|---|---|---|
| gateway token 计数 | `cost_tracker.py` 依赖 gateway 返回的 token 用量（input_tokens/output_tokens）。当前 gateway 响应是否包含此数据需验证——若缺失需在 gateway 层补充解析。 | 成本仪表盘 |
| `assess_sql` 路径复用 | runner 执行定时任务时需走与 `run_query` 相同的 assess_sql 路径（含成本估算）。需确保 runner 能拿到 `AppState` 和 `conn_id`。 | 定时任务 |
| query 指导书 schema 分支 | query 技能的 system_prompt 需显式写明"纯结构问题（有哪些表/什么字段）→ 跳过检索管线、不跑 SQL、直接用 get_schema 回答"，否则"有哪些表"会白跑一轮知识库检索。 | 技能指导书 |

---

## 10. 实施映射（代码文件 → 设计元素）

> 本节让文档自包含：每个设计元素对应到具体文件，实施时按此表定位代码。

### 10.1 后端工具层

| 工具 | 实现文件 | 注册方式 | 状态 |
|---|---|---|---|
| `run_query` | `app/ai/tools/sql.py` | `register()` 函数，import 触发 | ✅ 已有 |
| `run_dml` | `app/ai/tools/sql.py` | 同上 | ✅ 已有 |
| `draft_ddl` | `app/ai/tools/sql.py` | 同上 | ✅ 已有 |
| `get_schema` | `app/ai/tools/schema_tools.py` | 合并了旧 describe_table，可选 table 参数 | ✅ 已有 |
| `query_audit` | `app/ai/tools/query_audit.py` | 新建，独立文件 | ✅ 已有 |
| `ai_review` | `app/ai/tools/ai_review.py` | 新建，调 LLM 语义审查；**需补单管道+清单+审计**（§1.4 铁律 3） | ⚠️ 缺出网审计 |
| `kb_read` | `app/ai/tools/kb_read.py` | 新建 | ✅ 已有 |
| `kb_write` | `app/ai/tools/kb_write.py` | 新建，confirm=card | ✅ 已有 |
| `graph_read` | `app/ai/tools/graph_read.py` | 新建 | ✅ 已有 |
| `graph_write` | `app/ai/tools/graph_write.py` | 新建，confirm=card | ✅ 已有 |
| `manage_task` | `app/ai/tools/manage_task.py` | 新建 | ✅ 已有 |
| `suggest_followup` | `app/ai/tools/suggest_followup.py` | 新建，loop 自动调用 | ✅ 已有 |
| 注册汇总 | `app/ai/tools/__init__.py` | 所有工具 import + register() 在此 | ✅ 已有 |

### 10.2 后端技能层

| 技能 | 实现文件 | 状态 |
|---|---|---|
| 6 技能定义 | `app/ai/skills/builtin/__init__.py` | ✅ 已有，**需补 system_prompt 中的 schema 分支和降级话术（Q4）** |
| 技能注册/开关 | `app/ai/skills/registry.py` | ✅ 已有 |
| 技能数据模型 | `app/ai/skills/skill.py` | ✅ 已有 |

### 10.3 后端意图/路由/循环

| 模块 | 文件 | 说明 | 状态 |
|---|---|---|---|
| 意图识别 | `app/ai/preflight.py` | 6 意图（query/write/report/knowledge/scheduler/offtopic） | ✅ 已有，**需补"图谱"关键词** |
| 意图→技能映射 | `app/ai/agent/dispatcher.py` | INTENT_TO_SKILL 字典 + floor 检查 | ✅ 已有 |
| 主循环 | `app/ai/loop.py` | tool 执行 + SSE 事件；**需补 ai_review 出网审计** | ⚠️ 缺 ai_review 清单 |
| 上下文组装 | `app/ai/context.py` | system prompt + schema 摘要 | ✅ 已有 |

### 10.4 后端知识库/图谱/任务/成本

| 模块 | 文件 | 说明 | 状态 |
|---|---|---|---|
| 知识库存储 | `app/ai/knowledge/store.py` | SQLite docs+tags，每连接独立 | ✅ 已有 |
| 图谱存储 | `app/ai/graph/store.py` | SQLite edges + BFS 查询 | ✅ 已有 |
| 任务存储 | `app/ai/tasks/storage.py` | SQLite tasks+task_runs | ✅ 已有 |
| 任务 runner | `app/ai/tasks/runner.py` | **未实现**——需写：触发→assess_sql→成本闸→执行→审计（§2.1） | ⬜ 待做 |
| 成本追踪 | `app/ai/cost_tracker.py` | 记录每次 AI 调用的 token/成本 | ✅ 已有，**依赖 gateway 返回 token 数** |
| 安全闸门 | `app/safety/gate.py` | 本地规则引擎，assess_sql 三档 | ✅ 已有（不变） |
| B3 代号化 | `app/safety/codify.py` | codify/decodify 往返 | ✅ 已有（不变） |
| 脱敏管道 | `app/safety/redact.py` | redact_rows/redact_text | ✅ 已有（不变） |
| 清单 | `app/ai/manifest.py` | build_manifest | ✅ 已有（不变） |

### 10.5 后端 API 端点

| 端点 | 文件 | 状态 |
|---|---|---|
| `POST /api/v1/tasks` + CRUD | `app/api/tasks.py` | ✅ 已有 |
| `GET /api/v1/cost/summary\|daily` | `app/api/cost.py` | ✅ 已有 |
| `POST /api/v1/suggestions/initial` | `app/api/suggestions.py` | ✅ 已有 |
| 路由注册 | `app/main.py`（router include 循环） | ✅ 已有 |

### 10.6 前端

| 组件 | 文件 | 状态 |
|---|---|---|
| 技能开关（11 工具名） | `components/SkillPlaza.tsx` | ✅ 已更新 |
| 定时任务控制台 | `components/TasksConsole.tsx` | ✅ 已有 |
| 成本仪表盘 | `components/CostDashboard.tsx` | ✅ 已有 |
| 侧边栏导航（含 tasks/cost） | `components/AppLayout.tsx` | ✅ 已有 |
| View 类型（含 tasks/cost） | `store/ui.ts` | ✅ 已有 |
| i18n 中文 | `locales/zh-CN.ts` | ✅ 已更新 |
| i18n 英文 | `locales/en-US.ts` | ✅ 已更新 |

### 10.7 需补的实现缺口（按优先级）

| # | 缺口 | 对应设计 | 文件 | 状态 |
|---|---|---|---|---|
| 1 | ai_review 缺出网审计 | §1.4 铁律 3 | `tools/ai_review.py` + `loop.py` | ✅ 已补 manifest+audit（source=egress-review） |
| 2 | runner 未实现 | §2.1 定时任务信任模型 | `app/ai/tasks/runner.py` | ✅ 已实现：assess_sql→SELECT-only→执行→审计 |
| 3 | gateway token 计数 | §9 前置依赖 | `app/ai/gateway.py` | ⬜ 待做（依赖 gateway 返回 usage 字段） |
| 4 | query 指导书 schema 分支 | §9 前置依赖 | `skills/builtin/__init__.py` | ✅ 已补"纯结构问题跳过检索管线" |
| 5 | keyword "图谱" 收窄 | §5 | `preflight.py` _KEYWORD_RULES | ✅ 已收窄为"图谱/关联图/知识" |
