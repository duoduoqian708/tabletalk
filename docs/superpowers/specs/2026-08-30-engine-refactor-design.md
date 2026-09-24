# 引擎重构设计（阶段 2）：Plan-and-Execute + ReAct 分层引擎

> 日期：2026-08-30 · 状态：已批准 · 定位：对齐 `docs/knowledge-and-engine-design.md` §12–§19
> 前置：阶段 1（知识库收尾）已交付，全量 698 passed

---

## 1. 目标与现状差距

设计文档第二部分的引擎（§12–§19）在当前代码中仅部分存在：

| 设计点 | 现状 | 差距 |
|---|---|---|
| §12 Plan-and-Execute + ReAct | 单 ReAct 循环（chat_stream 580 行）+ 独立 report_stream | 无任务层、无 TaskPlan |
| §13 三轴剖面 | preflight 单值 `intent`（6 枚举） | 无 action/modality/trust/target 剖面 |
| §13.3 语义分解 | 无 | 复合请求不分解 |
| §14 任务循环 | 无 | 核心缺失 |
| §15 skill 配置 | Skill dataclass + 6 内置技能 + 白名单（已有基础） | 缺 match/termination/degradation；缺 general 兜底 |
| §16 安全横切 | 双墙完整（白名单 + 全局闸门） | **已符合，不动** |
| §17 审计 | 中央记账拦截器完整 | **已符合，不动** |
| §18 Context 契约 | 无统一对象 | 缺层间 I/O 契约 |
| §19 结果组装 | 无任务级组装 | 缺任务结果块 |

## 2. 关键决策（已批准）

1. **意图层**：`decompose.py` 一次 LLM 调用直接产出 TaskPlan（长度 ≥1），替代 preflight 单值 intent；关键词快判/mock 降级保留但输出同为"单任务 TaskPlan"；tags/followup/skip_retrieval/脱敏/清单/审计作为 plan 级辅助字段并入
2. **执行器**：从 chat_stream 提取单任务 ReAct 核心 → executor.py 任务循环（route→skill→ReAct→Context）；失败即停；写任务前校验依赖读成功
3. **report 并入任务循环**：report_stream 改造为 report 技能专属执行器（保留 report_id/snapshot/章节事件），由任务循环驱动；删除 loop.stream 的 mode 分流
4. **skill 配置对齐**：Skill 加 match/termination/degradation；内置 6 技能补 match；新增第 7 个 general 兜底；route(action, modality) 纯函数穷举 8 规则
5. **前端硬兼容**：现有 SSE 事件全保留，新增 task_start/task_done/task_result 为增量

## 3. 模块结构

```
backend/app/ai/
  plan.py           # E1: TaskSpec/TaskPlan + trust 派生 + action/modality 封闭枚举
  decompose.py      # E2: 意图分解（LLM TaskPlan / 关键词快判 / mock 降级）
  context_object.py # E3: Context 层间 I/O 契约
  executor.py       # E4: 任务循环执行器（route→skill→ReAct→Context；失败即停）
  loop.py           # E7: 变薄——单任务 ReAct 核心 + stream 入口（preflight→decompose→executor）
  report.py         # E7: report 技能专属执行器（并入任务循环）
  skills/           # E5: Skill 扩展字段 + general 技能 + route 纯函数
  preflight.py      # 保留：plan 级辅助字段（tags/followup/skip_retrieval）+ 脱敏/清单/审计管道
```

## 4. E1 · TaskPlan 数据模型（§13.1/13.2）

```python
# plan.py
ACTION = {"query", "write", "ddl", "kb", "schedule", "system", "unknown"}   # 封闭枚举
MODALITY = ["answer", "analyze", "report", "automate"]                       # 有序枚举（可升降级）
TRUST: dict[str, str] = {"query": "read", "write": "write", "ddl": "ddl"}   # trust 派生表（不分类）

@dataclass
class TaskSpec:
    action: str            # 封闭枚举，含 unknown 兜底
    modality: str          # 有序枚举
    target: dict = field(default_factory=dict)   # {tables?: [...], concept?: ...} 自由抽取
    # trust 派生，不入序列化

@dataclass
class TaskPlan:
    tasks: list[TaskSpec]
    tags: list[str]          # plan 级领域标签（preflight 保留）
    followup_tables: list[str]  # 追问轮种子（preflight 保留）
    skip_retrieval: bool     # 结构问答跳过检索管线
    degraded: bool           # 走了关键词/mock 降级
```

## 5. E2 · 意图分解（§13.2/13.3）

- **LLM 路径**：单次调用产出 `{"tasks": [{action, modality, target}], "tags": []}`；语义分解依据"是否独立操作"（不同表/动作拆；同一查询多指标不拆）
- **关键词快判**：现有 `_KEYWORD_RULES` 命中 → 单任务 TaskPlan（高特异度优先）
- **mock 降级**：单任务 query（保持零 LLM）
- **边界反例**（沿用 preflight 经验）："看看"不算 write；"你好，查一下订单"是 query 不是 offtopic；"生成月度报告"是 report；"顺便"类复合拆 2 任务
- 脱敏/清单/审计管道复用 preflight（plan 级一次）

## 6. E3 · Context 契约（§18）

```python
@dataclass
class Context:
    conn_id: str
    session_id: str | None
    tasks: list[TaskSpec]
    current_skill: str | None
    selected_tables: list[str]      # 图/知识库选出的表集合（跨层共享）
    sql_draft: str | None
    task_results: dict[str, Any]    # 前序任务结果（task.id → result）
    include_data: bool
    session_vars: dict[str, str]    # 会话变量（执行层替换）
```

## 7. E4 · 任务循环执行器（§14）

```
for task in plan.tasks:
    skill_id = route(task.action, task.modality)     # 纯函数（E5）
    ctx.current_skill = skill_id
    result = await run_task_react(ctx, skill_id, task)  # 单任务 ReAct（从 chat_stream 提取）
    ctx.task_results[task.id] = result
    if result.failed and (后续任务有写意图 or 依赖读失败):
        stop                                       # 失败即停
```

- 单任务 ReAct 保留现有能力：工具白名单、SSE 事件、确认协议、覆盖率、有界纠错、图校验
- 失败即停判定：当前任务失败 → 若剩余任务含 write/ddl → 停；否则继续（读任务失败不阻塞后续读）

## 8. E5 · Skill 配置对齐（§15）

- Skill 增加 `match: dict`（`{action, modality}`）、`termination: dict`（`{max_turns, done_when}`）、`degradation: str`
- 内置技能补 match；新增 general（read，工具 = get_schema/kb_read/graph_read/run_query）
- `route(action, modality)` 穷举 8 规则（§15.4）：(query, answer/analyze)→query；(query, report)→report；(query, automate)→schedule；(write,*)→write；(ddl,*)→ddl；(kb,*)→knowledge；(schedule,*)→schedule；(unknown,*)→general
- 内部技能 id 沿用现有命名（scheduler），route 表注释对齐设计命名

## 9. E6 · 结果组装（§19）

- 任务结果统一 `{type: "table"|"report"|"confirm"|"text", content, ...}`；按任务顺序编号
- SSE 增量事件：`task_start{id, skill, action}` / `task_done{id, type, content}` / `task_result{index, id, ...}`
- 现有事件协议零破坏（增量式）

## 10. E7 · 集成（§12）

- `loop.stream`：preflight（plan 级字段）→ decompose(TaskPlan) → executor（任务循环）；report 任务由 report 技能执行器处理
- `chat_stream` 变薄为单任务执行器（保留直接调用方兼容）
- mock 全链路冒烟：复合请求 → 2 任务 → 顺序执行 → 编号结果
- 全量回归（698 基线）+ SSE 协议兼容检查

## 11. 验收

- E1–E6 每项真实断言单测（无空断言）
- E7 全量 pytest 通过 + mock 冒烟（复合请求任务编号）+ 前端事件协议兼容
- 安全/审计零回归（双墙、中央记账不变）

## 12. 明确不做

- skill 自定义 UI、ScriptSpec 步骤引擎（v2）
- task 并行（设计明确顺序执行）
- SCD/非等值 join、一列多义条件归属（阶段 1 已后置）