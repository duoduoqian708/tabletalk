# AI 规划与执行 · 实施文档（Implementation Guide）

> **版本**：v1.0（2026-08-23）· 对应设计：`product-handbook/08-ai-planning-execution.md` v0.2
> **用法**：本文档是唯一的实施入口。开发（人类或 AI 助手）从 §1 了解怎么用本文档，从 §6 领任务，按 §7 的顺序与规范交付。
> **背景**：本计划源自 2026-08-22/23 的设计评审（完整决策记录见 `docs/pending-decisions/2026-08-22-ai-planning-execution.md`，D1~D15）。手册 08/02 已按评审结论更新；本文档负责"从现状到目标怎么干"。
> **维护约定**：任务完成一项就在 §6 勾掉一项（状态列改 ✅ 并注明 commit）；设计变更先改 08 再改本文，两者一起交付。

---

## 1. 怎么用本文档（新人 5 分钟）

1. **通读 §2~§4**（约 15 分钟）：目标架构、三条铁律、现状在哪。这是唯一需要通读的部分。
2. **从 §6 领一个任务**（按 §5 的推荐顺序，第一个任务是 WS1-T1）。每个任务卡包含：做什么、为什么、怎么改（设计要点）、动哪些文件、怎么算完成（验收标准）。
3. 领到任务后，**去手册读对应内容**（按 `product-handbook/README.md` 的 B 表）：任务卡里标了"必读"。
4. 实现：测试先行，门控 `cd backend && .venv/bin/python -m pytest -q` + `cd frontend && npm run typecheck && npm run build`；涉及星图/AI 轨交互的加 e2e。
5. 交付：对照任务卡验收标准逐条报告通过/未通过及证据；**测不过就是没做完**。
6. 收尾：行为变了就同步改手册（任务卡里标了"文档同步"），代码文档一起交付。

**遇到本文档与手册 08 冲突时**：08 赢方向（先改 08），本文档赢任务细节。遇到 §8 的"开放问题"所涉范围：停下来问负责人，别自行拍板。

---

## 2. 目标架构（干什么）

### 2.1 一句话

对话框是平台内一切操作的统一入口：查数、写数、结构问答、审计回顾、数据源与知识库管理；平台外话题拒答引导。AI 可调用的能力以注册的 tool 白名单为准，**未注册即禁止**。

### 2.2 数据流（目标态）

```
用户问题（含追问）
  │
  ├─ preflight（统一 owner，≤2s 预算，脱敏/清单/审计各恰一次）
  │    关键词快判 ──多数请求 0 次 LLM──▶ 直接出意图
  │    拿不准 ──▶ 一次 LLM 返回 {intent, tags}（拿不准当 query）
  │    超时/异常 ──▶ 降级关键词；追问轮：L2 种子并入上轮 card.tables
  │
  ├─ 意图 -> 技能路由（dispatcher 感知 enabled；不可用技能 -> 降级应答给出路）
  │
  ├─ offtopic ──▶ refusal 技能（云端引导式拒答，无工具；strict 降本地固定文案）
  ├─ 数据面（query/report/schema/write/ddl）
  │     └─ 组装上下文（候选子图 ≤20 表）-> loop（有界纠错）-> 闸门 -> 渲染
  └─ 控制面（audit_qa 起步，分批）
        └─ 平台工具（trust/confirm/audit，source=system_tool 审计）
```

### 2.3 意图集（两平面，v1 封闭集）

| 平面 | 意图 | 执行路径 |
|---|---|---|
| 数据面 | `query` | 检索 -> 单条 SQL -> 闸门 -> 卡片 |
| 数据面 | `report` | 章节化剧本（clarify->plan->execute->narrate） |
| 数据面 | `schema` | 直接答结构，跳过检索管线，不执行 SQL |
| 数据面 | `write` | run_dml 预览 -> 确认协议（WS4） |
| 数据面 | `ddl` | draft_ddl -> 发编辑器，永不执行 |
| 控制面 | `audit`（批次 1） | 平台工具（WS7） |
| 兜底 | `offtopic` | refusal 技能（WS2） |

判定纪律：单标签；**拿不准当 `query`**（误判代价矩阵见 08 §3.1）；写意图保守（"删掉/改成"才算 write）。

### 2.4 Skill / Tool 分层

* **Tool = 执行单元**：原子、无状态、自带信任等级（只读/变更/破坏性 + 闸门）。安全执行点永远在 tool 层。
* **Skill = 能力单元**：工具白名单 + 指导书 + 剧本 + 触发词，自身不执行。
* **技能只收窄不扩权**（工具集只做交集）；新技能≈零安全审查，新工具=安全评审。
* 注册规格：`{name, schema, trust, confirm, audit, registry(可开关)}`。

**v1 总量：14 tool / 10 skill**，完整两张表在 `08 §4.3/§4.4`（实施时以 08 为准，本文不复制）。永不注册：DDL 执行、数据源删除、模型配置修改、任意代码执行。

### 2.5 会话记忆

记忆随 session 走、服务端落库；**连接边界 = 会话边界**（切库换 session）；每张 sql_card 落 `result_id`（与 report `[r id]` 统一）；`load_result` 工具按 id 取工件；压缩 v1 只压缩不截断（骨架保全）；追问三路径（改写重查/工件操作/数据回加工）。

---

## 3. 三条铁律（违反任何一条都算实现错误）

1. **行数据不出网；工件进出模型上下文恒经脱敏管道**（privacy_mode 与行数上限照旧；会话存储/结果工件不是旁路；本地落盘不算出网）。
2. **模型可见世界恒代号**：schema、tool 入参回显、tool result 回喂一律代号化；真实名字只在闸门之后与展示层。
3. **技能只收窄不扩权；禁止靠缺席**：任何技能组合跑不出工具信任的并集；能力"不能做"的最强保证是 tool 不存在。

---

## 4. 现状差距盘点（2026-08-23 核对源码）

> 动手前复核行号未漂移。【现状】= 现在的代码；差距列 = 本计划要做的事。

| # | 模块 | 现状 | 差距 | WS |
|---|---|---|---|---|
| 1 | 意图层 | `intent.py` 三次独立 LLM 调用（classify_mode / classify_tags / dispatcher），各自内联脱敏/清单/审计；无 is_query | 合并为 `ai/preflight.py` 单调用 | WS1 |
| 2 | 技能/工具 | 5 个数据面工具已注册；内置 query/report；`update_skill` 支持 enabled；交集过滤已成立 | refusal/schema/write/ddl 技能；trust 注册规格；平台工具 | WS2/WS7 |
| 3 | 会话记忆 | `state.chats` 已有 session 落库（`api/ai.py` upsert/append_messages），**但 loop 上下文仍从前端 `req.messages` 组装**；无 result_id；无压缩 | 服务端接管历史组装；工件寻址；压缩 | WS3 |
| 4 | DML 确认 | run_dml 有 preview/blast/rollback 卡；无 confirm_token/pending；**审批 E2 批准执行绕过闸门（高优 bug）** | 三段协议 + 顺修 E2 | WS4 |
| 5 | 检索层 | 标签+向量+FK 2 跳融合已有；候选无封顶、无纠错、无覆盖率 | 封顶 20；纠错；覆盖率 | WS5 |
| 6 | 问题库 | 阈值 60 首尾 6 字符启发；**匹配在脱敏之后**（loop.py:108）；无确认步；保存无只读校验 | 原文匹配；确认卡；只读校验 | WS5 |
| 7 | B3 代号化 | codify/decodify 引擎完备；**执行前不还原 tool args**（敏感表 AI 查询报 no such table: t_17，核心路径断裂）；回喂真实列名 | 执行前还原；回喂代号化 | WS6 |
| 8 | 技能开关 | settings 抽屉有 skills 分节；enabled 持久化 | 地板常开；降级文案；勾选审计；团队交集 | WS7 |
| 9 | 前端 4 步 | ThinkPanel 默认折叠 + playSteps 1500ms 假定时器 | 真事件驱动、默认展开 | WS8 |
| 10 | 其他 | `/ai/chat` 有 kb_status!=ready 直接拒绝的闸（AI 被关死）；`/ai/selection` 硬编码文案 | 冷启动方案待定（开放问题 Q1） | — |

---

## 5. 实施顺序

```
WS1（preflight，地基）──▶ WS2（新技能）──▶ WS7（平台工具+开关）
WS6（B3 闭环，修核心 bug，可立即做，建议最先）
WS5（检索/问题库，独立）
WS3（会话改造，面最大）──▶ WS4（DML 协议）
WS8（前端）随各 WS 的前端任务分散落地
```

推荐开工序：**WS6（先修断裂的 bug）-> WS1 -> WS5 -> WS2 -> WS3 -> WS4 -> WS7**。每个 WS 可独立交付；WS 内任务按编号顺序做。

---

## 6. 任务卡（逐任务：做什么 / 怎么改 / 验收）

> 状态：☐ 未开始 · ◐ 进行中 · ✅ 完成（注 commit）。每张卡的"必读"是领任务后先读的手册内容。

### WS6 · B3 代号化闭环（先做：修核心路径断裂）

**必读**：02 B3（含 2026-08-23 修订）· 08 §8.2

**T6.1 执行前还原*** ✅ 2026-08-23 (WS6 done, B3 closed)
- 做什么：模型产出的 SQL（引用 `t_17/c_3`）在过闸门与执行前先还原为真实表/列名。
- 怎么改：`ai/tools/sql.py` 的 `_run_query`/`_run_dml` 入口处，对 `args["sql"]` 先 `decodify_text(data_dir, conn_id, sql)`，再 `assess_sql`、再执行。还原发生在闸门**之前**（闸门必须评估真实 SQL，代号名会让表级策略失效）。
- 验收：端到端测试——已确认敏感标签的表上，AI 查询不再报 `no such table: t_17`，返回真实数据；闸门审计记录的 sql 为真实语句。

**T6.2 回喂代号化*** ✅ 2026-08-23 (WS6 done, B3 closed)
- 做什么：tool result 回喂模型前，表/列名还原为代号（模型可见世界恒代号，铁律 2）。
- 怎么改：`_run_query` 组装 result 时，columns 经 `codify` 映射回代号（复用 `codify-{conn_id}.json`；新增列级 codify 辅助函数，若 `codify.py` 只有表级则扩展）。展示层（卡片）保持真实名（`loop.py` 现有 decodify 展示逻辑不变，注意避免双重还原/双重代号化）。
- 验收：单测断言回喂内容（`messages` 中 tool 消息的 JSON）无真实敏感表/列名；卡片展示为真实名。

**T6.3 审计真名与对应说明*** ✅ 2026-08-23 (WS6 done, B3 closed)
- 做什么：审计记录真实表名，并注明与清单代号的对应。
- 怎么改：审计调用处附 `tables`（真实名）+ `codified: bool` 或映射摘要字段。
- 验收：审计条目断言。

**T6.4 codify 单测补齐*** ✅ 2026-08-23 (WS6 done, B3 closed)
- 做什么：codify 模块现零测试覆盖（审计结论）。覆盖：生成/还原往返、同表同码稳定、冲突递增、列级 codify、双语 sensitive 标签。
- 验收：`tests/safety/test_codify.py` 全绿。

---

### WS1 · Preflight 统一意图层（地基）

**必读**：02 G1 · 08 §3 · 纪要 D3/D4/D5

**T1.1 preflight 模块*** ✅ 2026-08-23 (WS1 done, preflight+coverage)
- 做什么：新建 `backend/app/ai/preflight.py`，统一取代 `intent.py` 的 classify_mode/classify_tags 与 `dispatcher.py` 的 LLM 分支。
- 接口设计：

```python
@dataclass
class PreflightResult:
    intent: str            # query|report|schema|write|ddl|audit|offtopic
    tags: list[str]        # 命中的已确认领域标签
    degraded: bool         # True = 走了关键词降级（未调 LLM 或超时）
    is_followup: bool      # 追问轮
    followup_tables: list[str]  # 追问轮：上轮 card.tables（作 L2 种子）

async def preflight(state, conn_id, question, history_tail: list[dict]) -> PreflightResult
```

- 流程：①关键词快判（快判表见下）命中且高置信 -> 直接出；②否则一次 LLM 调用（`asyncio.wait_for` 2s 超时）：prompt 含问题（脱敏后）、对话尾部最后一条 user + 上轮 assistant 结尾 200 字、已确认标签清单、六类意图定义与相邻对边界例句，要求返回 JSON `{intent, tags}`；③解析失败/超时 -> 关键词快判兜底；④全程只做一次 `redact_text`、一次 `build_manifest`（source=egress-intent）、一条 egress 审计。
- 关键词快判表（初版，集中放在模块顶部常量便于测试）：`有哪些表|什么结构|表关系` -> schema；`删掉|改成|更新.*为|插入` -> write；`加.*列|建.*表|建索引|删列` -> ddl；`报告|出一份|趋势分析|概览` -> report；`你好|谢谢|天气|笑话` -> offtopic；`查|统计|多少|平均|按.*月` -> query；都不中 -> 调 LLM。
- 验收：单测——六类意图各≥2 正例 + 关键跨类反例（"看看测试订单"≠write）；超时降级路径（mock 一个慢 provider）；"拿不准当 query"（LLM 返回非法值时落 query）；egress 审计恰一条；`intent.py` 不再含内联 manifest/审计代码。

**T1.2 意图->技能映射与 enabled 感知*** ✅ 2026-08-23 (WS1 done, preflight+coverage)
- 做什么：`agent/dispatcher.py` 改为消费 PreflightResult；路由表 `INTENT_TO_SKILL`；目标技能被禁用时返回降级应答事件（文案："此能力已关闭，可在设置中开启"），**不产生死胡同**。
- 验收：单测——禁用 write 技能后"删掉测试订单"得到降级文案；地板技能（query/refusal）无法禁用。

**T1.3 追问轮路由*** ✅ 2026-08-23 (WS1 done, preflight+coverage)
- 做什么：preflight 判定 is_followup（当前句无领域词、或以"那|再|换成|也"开头、或上轮存在 sql_card），`followup_tables` 取上轮卡片的表集合；`context.py: assemble_context_full` 接受 seeds 参数并入候选。
- 过渡实现：上轮卡片从 `req.messages` 里取（前端历史里有 card 结构）；WS3 完成后改从 session store 取。
- 验收：单测——"那按周统计呢"的候选表包含上轮表。

**T1.4 意图质量度量*** ✅ 2026-08-23 (WS1 done, preflight+coverage)
- 做什么：done 时对比 PreflightResult.intent 与实际执行路径（哪个技能的哪些 tool 被调用），不一致率写入审计（`intent_mismatch: bool` 字段）。
- 验收：集成测试构造一次误判（mock LLM 故意返回错意图）断言审计字段。

**T1.5 覆盖率先行*** ✅ 2026-08-23 (WS1 done, preflight+coverage)（小任务，随 T1.1 一起交付）
- 做什么：done 时用 sqlglot 解析最终 SQL 的表集合，与 `candidate_tables` 对比，覆盖率入 `context_meta` 与审计。
- 验收：审计条目含 coverage 字段；空候选/全命中两个断言。

---

### WS5 · 检索层与问题库（独立，WS1 后做）

**必读**：02 G5、C6（含修订）· 08 §7 · 07 §6/§7

**T5.1 候选封顶*** ✅ 2026-08-23 (WS5 done)
- 做什么：融合 + 2 跳扩展后候选封顶 20 张；砍表顺序：标签命中 > 向量命中 > 2 跳外围（按距 seed 的跳数）。
- 怎么改：`context.py` 中 expand 之后加 `_cap_candidates(routed, tag_tables, vec_tables, limit=20)`：排序键 `(是否标签命中, 是否向量命中, -hop)`，稳定排序后切 20。封顶数进 `context_meta.capped: true`（防"静默截断"，08 §非目标纪律）。
- 验收：单测——构造 30+ 候选断言只剩 20 且优先级正确；`capped` 标志正确。

**T5.2 有界纠错*** ✅ 2026-08-23 (WS5 done)
- 做什么：`run_query` 报 no such table/column 时，向模型注入一次完整表清单，允许重写一次（计入 MAX_TURNS）。
- 怎么改：`tools/sql.py` 的 `_run_query` 捕获执行异常，若错误文本匹配 `no such table|no such column|doesn't exist|Unknown column`（方言覆盖），tool result 改为 `{error, available_tables: [...全库表名]}` 并附提示"SQL 引用了候选之外的表，以下是全部表清单，请重写"；非此类错误原样透出。注入仅每轮对话一次（session 或 messages 内计数）。
- 验收：单测——错表 SQL 后模型（mock 断言 tool result 内容）能拿到表清单；清单注入不重复。

**T5.3 问题库：原文匹配*** ✅ 2026-08-23 (WS5 done)
- 做什么：修复 `loop.py:108` 顺序——`state.questions.match(conn_id, user_text)` 移到 redact_text **之前**，用原文匹配；脱敏只管出网。
- 验收：单测——问题含手机号（保存时原文含号、提问时含同一号码）可命中。

**T5.4 问题库：命中确认卡*** ✅ 2026-08-23 (WS5 done)
- 做什么：命中后不直接执行，SSE 下发 `sql_card`（`question_library: true, question_id, sql, sub: "问题库命中 · 零模型调用"`，**不附 result**），前端渲染确认卡；用户点"执行"后走 `POST /query`（sql + origin=ai）正常过闸门执行；执行后照旧审计 source=question_library。
- 注意：确认前的命中卡本身不产生 egress（零模型调用不变），manifest 合成仍随命中卡下发（provider=local）。
- 验收：e2e——保存->再问->命中卡->点执行->结果；不点不执行。

**T5.5 问题库：保存只读校验*** ✅ 2026-08-23 (WS5 done)
- 做什么：`core/questions.py: save()` 增加 `assess_sql(sql, dialect, Origin.AI)` 校验，非 ALLOW 拒绝保存（提示"问题库仅收只读查询"）。
- 验收：单测——UPDATE 语句保存被拒；SELECT 保存成功。

**T5.6 阈值可配与误命中用例*** ✅ 2026-08-23 (WS5 done)
- 做什么：match 阈值 60 提为连接级可配（settings，默认 60 不变）；补误命中反例用例集（与负责人过一遍具体用例，见开放问题 Q3）。
- 验收：反例用例全部不命中。

---

### WS2 · 新内置技能（依赖 WS1）

**必读**：02 G4 · 08 §3.4/§4.4 · 03 §3

**T2.1 refusal 技能*** ✅ 2026-08-22 (WS2 done, 本轮提交内)
- 做什么：新增内置技能（无工具）。prompt 草稿（实现时按 08 §3.4 精化）：

```
你是 TableTalk 的数据库助手引导员。用户的问题与数据库/本平台无关。
你的唯一任务：①一句话礼貌说明你不处理此类问题；②给出 2~3 个基于当前数据库
（{connection_name}，领域：{confirmed_tags}）的具体建议问题。
禁止：回答问题本身；延伸话题；编造数据库里不存在的内容。
```

- strict 档：不调模型，返回本地固定文案模板（i18n 词典先行，中英双份）。
- 验收：e2e——"天气怎么样"得到拒答 + 引导（引导引用真实标签）；strict 下离线拒答；每次云端拒答调用有 manifest + egress。

**T2.2 schema 技能*** ✅ 2026-08-22 (WS2 done, 本轮提交内)
- 做什么：意图为 schema 时跳过 assemble_context_full 的向量召回，直接给 schema 摘要（`get_schema` 数据），无工具或仅 get_schema/describe_table，模型组织成结构化回答；不产生 sql_card。
- 验收：单测——schema 意图时零向量召回（断言 vector_route_tables 未被调用）、零 SQL 卡。

**T2.3 write / ddl 技能*** ✅ 2026-08-22 (WS2 done, 本轮提交内)
- 做什么：按 08 §4.4 工具集注册两个内置技能；write 的 system_prompt 前置写操作态势说明（"这是写操作，将先预览后人工确认"）；ddl 强调草案边界。
- 验收：单测——遍历 D15 表断言每个技能的 `skill_tool_schemas` 输出与 08 §4.4 一致。

**T2.4 C2 建议过滤*** ✅ 2026-08-22 (WS2 done, 本轮提交内)
- 做什么：前端 `AiRail.tsx` 的 dynamicSugs 只引用 enabled 技能涉及的能力。
- 验收：e2e——关闭 report 技能后建议列表不含报告类问题。

---

### WS3 · 服务端会话与工件（面最大，可与 WS5 并行）

**必读**：02 G2 · 08 §5 · 纪要 D6/D7/D8

**T3.1 服务端历史组装** ✅
- 做什么：loop 从 `state.chats` 读 session 历史组装 messages；`req.messages` 降级为兼容通道（session 无历史时使用，如旧会话/测试直连）。
- 怎么改：`api/ai.py` 已有 `state.chats.upsert/append_messages`；新增 `state.chats.get_messages(session_id)`（含 kind 元数据）；`loop.py` 优先服务端历史。落库侧补齐：当前 `_events_to_messages` 只落 user/text/sql_card/report/clarify，需补 think（可跳过）、gate、stage 元数据进 kind。
- 验收：集成测试——同 session 连续两问，第二问的 messages 来自服务端（断言不含前端重复传的历史）；旧请求带 req.messages 仍可工作。

**T3.2 result_id 工件** ✅
- 做什么：sql_card 落 `result_id`；结果（columns/types/row_count/truncated/elapsed + capped rows，上限与 run_query 一致）持久化到 session 存储；report 章节结果同机制（统一 [r id]）。
- 怎么改：`chats` 存储加 `artifacts` 表（session_id, result_id, data JSON）；`_events_to_messages` 或 SSE 产出侧在 sql_card 时写 artifact。
- 验收：单测——两轮查询后按 id 可取回两个工件；行数超过上限时截断存储。

**T3.3 load_result 工具** ✅
- 做什么：新增 tool（按 08 §4.3 行 6）：入参 `{result_id}`；返回列名/行数/样例行（上限 N 行）；**standard 档 rows 过 redact_rows、strict 档 rows 一律不返回**（只回 columns+row_count）；strict/open/standard 三档复用 `tools/sql.py` 现有三档逻辑，抽公共函数避免复制。
- 验收：单测——standard 回喂被 redact；strict 拦行数据；不存在/跨 session 的 id 报错友好。

**T3.4 压缩 v1** ✅
- 做什么：新 `ai/compress.py`。策略（机械优先）：
  1. 估算 token（字符数近似即可，中文 1 字≈1.5 token）；低于阈值（暂 12k）不压缩。
  2. 超限：从最旧轮次开始将正文压为一句摘要（本地截断，非 LLM），**卡片骨架（result_id/表名/verdict/rowcount）原样保留**；当前轮与 pending_dml 永不动。
  3. 机械压完仍超限（极端长会话）：LLM 摘要旧轮次——此调用走单管道（脱敏输入、manifest、egress 审计 source=egress-compress）。
- 验收：单测——压缩后骨架含全部 result_id；当前轮未被动；LLM 摘要路径有 egress 审计；机械路径无任何模型调用。

**T3.5 切库换 session** ✅
- 做什么：前端切换连接时若当前 session 属于旧连接，自动开新 session（不发混合上下文请求）；后端 `POST /ai/chat` 校验 session 的 connection_id 与 req.connection_id 一致，不一致返回明确错误（双保险）。
- 验收：e2e——切库后追问不带旧库上下文；后端校验单测。

---

### WS4 · DML 确认协议（依赖 WS3 的 session 存储）

**必读**：02 G3 · 08 §6 · 03 §6 · 纪要 D11

**T4.1 confirm_token 状态机** ☐
- 做什么：token 生成与校验工具（可放 `safety/confirm.py`）。
- 设计：`token = uuid4().hex`；session 存 `pending_dml = {token, sql_hash(sha256), preview, rollback, created_at, expires_at(+10min), consumed: false}`；校验：未过期 + 未消费 + sql_hash 一致 -> 置 consumed 并放行；任何一条不满足 -> 拒绝并要求重走 preview。
- 验收：单测——合法确认、过期拒绝、二次消费拒绝、SQL 哈希不符拒绝（TOCTOU）。

**T4.2 loop 终止与确认卡下发** ☐
- 做什么：`run_dml` 返回 REVIEW 结果时，loop 当轮终止（不再进入下一 MAX_TURNS 轮次，模型不再发言）；SSE 下发 sql_card 附 `confirm_token` 与 `expires_in`；pending_dml 写入 session（豁免压缩，T3.4 已保）。
- 验收：e2e——run_dml 后无后续模型输出；卡片含 token。

**T4.3 确认执行通路** ☐
- 做什么：`POST /query` 接受 `confirm_token`（与 confirm=true 同传）：校验 token -> **重新 `assess_sql`**（确认不是通行证）-> 执行 -> 审计带 `confirm_token` + `turn_id`（与 preview 审计条目可互相检索）。
- 验收：集成——确认执行的审计条目能关联 preview 条目（token 相同）；确认时 SQL 被篡改则拒绝。

**T4.4 取消写回历史** ☐
- 做什么：取消/过期后，向 session 追加一条系统可见消息（如 kind=system, "用户取消了该写操作"），下轮模型上下文可见；同时清除 pending_dml。
- 验收：单测——取消后下轮 messages 含取消信号。

**T4.5 顺修 E2 审批绕闸** ☐
- 做什么：`api/approvals.py` 批准执行路径补 `assess_sql(Origin.AI)` + read_only 检查 + confirm 语义（仅 ALLOW/已确认 REVIEW 可执行）；创建审批时也先过闸门记录 verdict。
- 验收：单测——批准的无 WHERE UPDATE 被闸门拦（这条是审计 2026-08-21 的高优 bug 修复）。

---

### WS7 · 平台工具与技能开关（依赖 WS1 的 enabled 感知）

**必读**：02 G6/G7 · 08 §4.2/§4.5 · 纪要 D13/D14

**T7.1 工具注册规格扩展** ☐
- 做什么：`tools/registry.py` 的工具定义加 `trust: readonly|mutating|destructive`、`confirm: none|card|admin`、`audit_source` 元数据；注册校验：缺 trust 拒绝注册（含启动时自检）。
- 验收：单测——缺元数据注册被拒；既有 5 工具补挂后通过自检。

**T7.2 query_audit / get_last_operations** ☐
- 做什么：两个平台工具。`query_audit`：读 `audit.log` JSONL，过滤（时间范围/origin/verdict/connection），返回结构化条目（分页，默认 20 条）；`get_last_operations`：当前用户当前 session 连接最近 N 条执行的语句。结果回喂过 `redact_rows`/`redact_text`（审计里含 SQL 原文，按 standard 档处理）。每次调用审计 `source=system_tool`。
- 验收：e2e——"我刚才干了什么"返回正确回顾；单测——回喂内容经脱敏；审计条目存在。

**T7.3 audit_qa 技能** ☐
- 做什么：按 08 §4.4 注册技能（query_audit, get_last_operations, load_result）；system_prompt 指导审计问答（时间范围、verdict 含义、引导去审计页看全量）。
- 验收：单测——工具集与 D15 一致；意图 audit 路由到此技能。

**T7.4 技能开关完善** ☐
- 做什么：①`skills/registry.py` 或 settings 层加地板常量（query/refusal 禁止 disable，校验在 update_skill 与 API 两层）；②设置 API 的技能开关变更写审计（source=settings, skill_id, enabled）；③团队模式交集：组织策略（admin 设置）∩ 个人勾选（E4 角色体系上实现，单人模式合一）。
- 验收：单测——地板不可关；勾选产生审计事件；成员勾选不能恢复管理员禁用项。

---

### WS8 · 前端（随各 WS 落地，不单独立项）

**必读**：03 §3/§6 · 04（颜色 token）

**T8.1 真 4 步** ☐（随 WS1）
- `AiRail.tsx`：移除 `playSteps` 及 1500ms 定时器；ThinkPanel 默认展开；4 段由 stage/manifest/think/sql_card/gate 真事件驱动；intent/retrieval 的 detail 绑定 `context_meta`。拒答轮不渲染 4 步。

**T8.2 确认卡** ☐（随 WS4/WS5）
- DML 确认卡三态：执行（文案带预览行数，"确认更新 238 行"）/取消/过期提示；问题库命中卡（T5.4）。文案全部先入 i18n 词典。

**T8.3 技能开关 UI** ☐（随 WS7）
- SettingsDrawer skills 分节：地板技能置灰+说明；其余勾选；变更 toast 确认。

---

## 7. 全局规范（每个任务都适用）

1. **TDD**：先写失败测试再实现；门控全绿才算完（`pytest -q` / `typecheck` / `build`；交互改动加 e2e，星图改动跑像素断言脚本）。
2. **手册硬规则**（README C 节全文适用）：颜色引用 tokens.css 变量禁硬编码；文案先入 i18n 词典再写组件（中英双份）；守门逻辑本地纯函数；单管道约束；改行为必同步文档。
3. **每张卡的"文档同步"**：实现完成后检查对应手册节（08 / 02 条目 / 03）描述是否仍准确，08 里的【目标】标记项完成后改为【现状】。
4. **不做的事**（除非负责人明确变更）：平台工具批次 2/3（连接接入、标注、导出）不在本计划内；KB 冷启动方案等开放问题 Q1；e2e 登录回归修复是独立事项（审计 E4 项），不在本计划但会挡 e2e 验证，遇到时先报告。

---

## 8. 开放问题（遇到即停，问负责人）

| # | 问题 | 现状 | 影响 |
|---|---|---|---|
| Q1 | KB 冷启动：后台构建+进度事件 vs 首轮降级 schema-only | 现状 `/ai/chat` kb_status!=ready 直接拒绝（AI 被关死） | WS1 验收的"首包"体验；实现到 `/ai/chat` 闸时停下确认 |
| Q2 | preflight 超时预算具体值 | 暂定 2s（08 §1.3） | T1.1 的 wait_for 参数 |
| Q3 | 问题库误命中反例的具体用例集 | 待与负责人过一遍 | T5.6 验收 |
| Q4 | `/ai/selection` 恢复真实模型调用 or 维持降级 | 现状硬编码三条文案（审计 B1#6） | 不在本计划内，提及防混淆 |

---

## 9. 决策索引（背景速查）

D1 平台全能助手定位 · D2 云端引导式拒答 · D3 preflight 合并单调用 · D4 拿不准当 query · D5 两平面意图集 · D6 服务端会话与 result_id · D7 压缩策略 · D8 追问三路径 · D9 统一脱敏管道与代号不变式 · D10 检索封顶/纠错/覆盖率 · D11 DML 三段确认协议 · D12 skill/tool 分层原则 · D13 平台功能注册制 · D14 技能开关四细则 · D15 14 tool / 10 skill 总表。

完整推理过程见 `docs/pending-decisions/2026-08-22-ai-planning-execution.md`（归档用，实施不必读）。
