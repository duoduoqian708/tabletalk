# 技能编排框架（Skill Orchestration）设计文档

日期：2026-08-18 · 状态：设计定稿（待实现）· 前序决策见 `2026-08-18-trust-completeness-prd.md` §7.3（分步输出）

## 1. 目标

把 TABLETALK 从"一个数据库客户端"升级为**可扩展的 agent 平台**：能力以"技能（skill）"为单元可配置、可插拔、可增删，Agent 通过意图识别调度不同技能完成不同任务（查数据 / 报表 / 查变更历史 / 知识库问答 / SQL 诊断……），查询数据库仍是主场景但不被它锁死。

## 2. 核心概念：两层模型（Tool 原子层 × Skill 剧本层）

把平台拆成**两层明确分工**（这是本设计的骨架，比"Agent 直接有工具"更可扩展）：

### 2.1 Tool（工具）= 独立原子能力单元
每个 Tool 干**一件事**、参数自洽、可独立复用，是"积木"。**Tool 不含业务流程，只含单一能力**。

| Tool | 能力 | 只读 | 复用者 |
|---|---|---|---|
| `run_query` | 执行只读 SQL，返回列名+行数(默认)/明细(opt-in) | ✅ | query/report/kb |
| `run_dml` | 执行写 SQL（闸门确认后） | 写需确认 | query |
| `get_schema` | 取表结构摘要 | ✅ | query/report/kb/diagnose |
| `describe_table` | 单表列定义/注释 | ✅ | query/report/kb |
| `draft_ddl` | 生成 DDL 草稿(不执行) | 草稿 | query |
| `query_history` | 查审计日志(某表/时间/来源变更历史) | ✅ | history |
| `diagnose_sql` | 对用户 SQL 做解释/优化/风险提示(sqlglot) | ✅ | diagnose |

> 补充：原子 Tool 层**还隐含"编排原语"**——如"意图判别""候选表路由"这类步骤，若被跨技能复用，也做成内部原语（详见 §4），保证 Skill 剧本引用的是稳定的原子单元而非散状代码。

### 2.2 Skill（技能）= 使用指导书（编排剧本）
Skill 不是"一个函数"，而是**一段可配置的流程描述**：告诉 Agent"本场景该按什么顺序调用哪些 Tool、每步产物如何流转、受什么安全约束"。**Skill = 剧本，不是代码 if-else 链**——这正是可插拔、可增删的真正含义。

```python
@dataclass
class Skill:
    id: str                  # "query" / "report" / ...
    name: str
    description: str         # 意图识别用的能力描述
    tools: list[str]         # 本剧本可启用的原子 Tool 名
    script: ScriptSpec       # ★ 编排剧本：步骤序列 + 产物流转 + 安全约束
    system_prompt: str
    builtin: bool
    read_only: bool
```

### 2.3 产物流转（剧本的灵魂）
Skill 剧本描述的是"每一步工具调用的输入（来自上一步产物）→ 输出（传给下一步）"：
- 目的现在是**给前端四步展示 + 模型思考**的数据（意图 → 候选表 → SQL → verdict）
- Skill 之间不共享状态（无耦合，各自独立流程）
- Agent 循环执行为脚本，产物流转决定了"分步输出"（PRD §7.3）有真实阶段可展示

### 2.4 一个 Agent 调度
- **Agent（ReAct 循环）**：持有全部注册 Skill。
- **意图识别 dispatcher**：看问题 → 选一个 Skill。
- **选中 Skill 的剧本**决定本次循环"用哪些工具、按什么顺序、每步产物怎么流"。
- **安全底座**：剧本无论怎么编排，任何 SQL 执行都过统一闸门（铁律）。

这套"Tool 原子 × Skill 剧本 × 产物流转"就是你要的——**查询场景 = Query Skill 的剧本把几个 Tool 按四步串起来**。

## 3. 与既有架构的关系（顺水推舟，非重写）

| 既有点 | 在框架里的位置 |
|---|---|
| `app/ai/loop.py` chat_stream | Agent 核心循环，不动 |
| `app/ai/tools.py` TOOL_SCHEMAS 5 工具 | 首批底层工具（get_schema/describe_table/run_query/run_dml/draft_ddl，等同 SQL 技能的工具集） |
| `app/ai/report.py` report_stream | **第一个"技能"雏形**：独立只读工具集 + report_system_prompt + 自己的 generator |
| `app/ai/intent.py` classify_mode/classify_tags | 意图识别基础：从 query/report 二分升级为多技能路由 |
| `app/audit/`、`app/knowledge/`、sqlglot | history / kb / diagnose 技能背后的现有资产 |

**这说明**：report 已经是"技能"的样板，本次改造是把它**泛化成可配置的注册表**，不是凭空造框架。

## 4. 架构分层

```
                        ┌───────────────────────────────┐
                        │         意图识别 (dispatcher)      │
                        │  LLM 判定 + 关键词兜底 → 选技能      │
                        └───────────────────────────────┘
                                        │
                         ┌──────────────▼──────────────┐
                         │      Skill Registry 注册表      │
                         │  name/desc/tools/prompt/只读   │
                         │  (可配置增删 · 内置+自定义)       │
                         └──────────────┬──────────────┘
                                        │  选中技能的 tools + prompt
                         ┌──────────────▼──────────────┐
                         │      Agent 循环 (loop.py)      │
                         │  ReAct: 思考→调工具→观察→继续      │
                         └──────────────┬──────────────┘
                                        │  execute_tool 分发
                         ┌──────────────▼──────────────┐
                         │        统一工具层(共享能力)       │
                         │  run_query / run_dml / ...     │
                         └──────────────┬──────────────┘
                                        │  ALL 工具执行必经：
                         ┌──────────────▼──────────────┐
                         │     安全闸门(本地·模型无关)         │
                         │  读直通 / 写确认 / DDL仅手动      │
                         └───────────────────────────────┘
```

**铁律**：安全闸门是所有技能的统一底座，**任何技能发出的 SQL 执行都必须过同一闸门**。技能化不弱化安全。

## 5. 技能注册表（可插拔核心）

### 技能定义（数据结构）
```python
@dataclass
class ScriptStep:
    id: str                # "intent" / "retrieval" / "sql_gen" / "gate"
    label: str             # 前端展示名，如 "意图分解"
    tool: str | None       # 本步调用的原子 Tool 名（可为 None，表示内部原语步骤）
    input_from: str | None # 输入来源（上一步产物字段）
    output: str | None     # 本步产物字段名
    constraint: str | None # 安全/流程约束（如"写操作必须确认"）

@dataclass
class ScriptSpec:
    steps: list[ScriptStep]   # 剧本步骤序列 = 四步/章节编排
    requires_schema: bool     # 是否需要 schema 上下文
    requires_history: bool    # 是否需要审计历史上下文

@dataclass
class Skill:
    id: str                      # 如 "query" / "report" / "history"
    name: str
    description: str             # 意图识别用的能力描述
    tools: list[str]             # 本剧本可启用的原子 Tool 名
    script: ScriptSpec           # ★ 编排剧本（工具调度顺序 + 产物流转 + 约束）
    system_prompt: str
    builtin: bool                # 内置（不可删除）或用户自定义
    read_only: bool              # 是否物理只读（report 只读）
```

### 注册表实现
- `app/ai/skills.py`：`SkillRegistry`——持有技能列表；`register()` 增、`list()`/`get()`、`remove()`（非内置）。
- 内置技能从代码注册（不可删）；自定义技能经配置（将支持 UI/配置增删，V1 先驻留代码注册 + 配置化预案）。
- **持久化**：内置在代码、自定义进 runtime settings（类似 ai_models 的 `_PERSISTED_KEYS`）。

### 首批内置技能（V1）
| id | 名称 | 工具 | 只读 | 说明 |
|---|---|---|---|---|
| query | 执行 SQL | run_query/run_dml/get_schema/describe_table/draft_ddl | 否(写需确认) | 主场景，读写合并(安全闸门分级) |
| report | 数据分析报告 | get_schema/describe_table/run_query | ✅ | 泛化现有 report_stream |
| history | 查询变更历史 | audit 类工具(get_audit/query_history) | ✅ | 基于审计日志，"这表改过什么" |
| kb | 知识库问答 | get_schema/describe_table/run_query | ✅ | 表/字段语义、标签、图谱 |
| diagnose | SQL 诊断 | (解析类,直接对用户 SQL) | ✅ | 解释/优化/风险提示 |

- **ops（数据运维/巡检）V1 不做**：真本事是后台自主运行+失败恢复+多智能体，属 V1.5；V1 只可能给"影响评估"形态（见 §10 非目标）。

## 6. 意图识别（dispatcher）——命门

`app/ai/intent.py` 的 `classify_mode` 从 query/report 二分，升级为**多技能路由 `dispatch_skill(state, question) -> skill_id`**：
- **真实 LLM**：给"可用技能 + 描述清单"，让模型返回一个 skill_id（拿不准回 query——保守默认主场景）。零定制：技能描述来自注册表，不由代码写死场景。
- **mock/降级**：关键词回退（复用于现有关键词 + 技能注册表的 keyword hint）。
- **兜底**：任何识别失败/超时 → 默认 `query` 技能（保证永远有得回答）。

**设计约束**：意图识别只做"选技能"这层；具体的领域标签仍走 `classify_tags`（标签路由不变），两者分层。

## 7. 工具层与 MCP（function-call 优先，MCP 预留）

- **V1 只用 function-call**：技能的工具描述为结构化 JSON Schema，`execute_tool` 按名分发。自研循环直接调 Python，不经外部协议。
- **统一工具层 = 未来 MCP 出口**：工具 schema 用标准 JSON Schema 组织，将来要 MCP 时在工具层外加"**MCP 适配器**"（把 schema 翻译成 MCP 工具、执行转发给 execute_tool）即可，逻辑不变。
- **为什么现在不做 MCP**：MCP 是"让外部 agent（Claude/Cursor）调 TABLETALK"用的，不是给我们自己用；团队网关/开放平台（V1.5+）才触发。设计预留、当下零负担。

## 8. Agent 调度流程（query 技能 = 四步剧本串 Tool）

query 技能的剧本（`ScriptSpec`）正是四步，每步把产物流转到下一步，最终喂给前端做"分步展示"：

```
Query Skill 剧本 steps:
  step1 intent     (tool=None    内部原语)  → 产出 intent       ← 识别问题类型
  step2 retrieval  (tool=get_schema)
                   input_from=intent        → 产出 candidate_tables  ← 标签路由圈候选表
  step3 sql_gen    (tool=run_query)
                   input_from=candidate_tables → 产出 sql        ← 模型一步生成 SQL(效率引导)
  step4 gate       (tool=run_query)
                   input_from=sql           → 产出 verdict+result ← 闸门评估+审计+执行

实际流程:
user: "最近30天退货率最高的10个产品"
  → dispatch_skill → query
  → 按剧本加载 query 技能: tools + query_system_prompt + steps
  → step1 intent: 意图分解（内部原语）
  → step2 retrieval: get_schema → 标签路由 → 候选表清单（喂给模型 + 前端"检索"阶段展示）
  → step3 sql_gen: run_query 一步写出最终 SQL（效率引导生效）
  → step4 gate: 闸门评估 → ALLOW(读) 或 REVIEW(写需确认) → 执行 → 审计(source=loop_internal)
  → 前端四步真实阶段展示: 意图 / 候选表 / SQL / verdict
```

**产物流转**正是前端"分步输出"（PRD §7.3）的数据来源——每一阶段有真实产物可展示，而非 mock 播放器。

## 9. 前端：四步真实阶段展示（呼应"分步输出"决策）

四步（意图 / 检索定位 / SQL 生成 / 安全评估）由后端 SSE 的**真实阶段事件**驱动，而非 mock 播放器：
- 后端新增 `{type:"stage", stage:intent, ...}` 与 `{type:"stage", stage:retrieval, tables:[...]}` 事件（暴露 classify_tags + route_tables 的真实结果）
- SQL 生成、安全评估的状态由既有 `sql_card`（真实 SQL + 真实 verdict）归位
- 前端 `StepsPanel` 移除 `THINK_LINES` mock 播放器（零定制红线），改为"事件驱动 + 缺真实数据的步骤保持 pending"
- 检索阶段展示**候选表清单**——用户可见"AI 圈了哪些表"，是信任证据（呼应 PRD §3 数字回溯 / §7.3 分步输出）

## 10. 非目标（V1 明确不做）

- **MCP 协议对接**（只做统一工具层 + 预留适配器）
- **ops/数据巡检技能**（后台自主运行是 V1.5 多智能体登记场景；V1 可能做只读"影响评估"形态，不自动化）
- **用户自定义技能的全 UI 编排**（V1 内置代码注册 + 配置化预案；UI 可视化编排 V1.5）
- **多智能体并行/独立失败域**（单 Agent 单选技能调度；多智能体是登记的未来场景）
- **不弱化安全**：任何技能 SQL 均过统一闸门；`read_only`/敏感名单/审计在技能化后依然生效

## 11. 实施顺序（提交 plan 时细化）

1. **技能注册表 + Skill 模型**（`app/ai/skills.py` + 测试）
2. **统一工具层**（确认 5 工具 schema 标准化；execute_tool 按 skill 上下文分发）
3. **意图调度升级**（`dispatch_skill` 多技能路由 + mock 兜底 + 测试）
4. **history / kb / diagnose 三个新技能的工具与 system prompt**（复用审计/知识库/sqlglot 资产）
5. **GPT 后端阶段事件 + 前端四步真实展示**（移除 mock 播放器）
6. **全量验证**（后端 pytest + 前端 typecheck/build + E2E：六个技能各自跑通 + 安全闸门在所有技能下生效）

## 12. 与决策总账的关系

落实 PRD §4 原则 5（机制 vs 内容分离——技能内容由模型/注册表动态生成而非硬编码）、§7.3（分步输出真实阶段产物）、§7.9/§7.10（安全闸门统一底座、HITL 不变）。
