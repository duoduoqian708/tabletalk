# 08 · AI 规划与执行（AI Planning & Execution）

> **状态**：v0.2（2026-08-23 按 v0.1 评审结论重写）· 读者：本仓人类与 AI 助手
> **性质**：`05`/`07` 的战术展开--05 定架构原则、07 定知识库，本篇定 **AI 如何做决策与如何把决策执行到底**。实现前必审本篇；代码与本文冲突时以本文为准（先改本文再改代码，`01 §9` 决策日志留痕）。
> **修订来源**：v0.1 评审纪要 `docs/pending-decisions/2026-08-22-ai-planning-execution.md`（D1~D15 决策、现状差距盘点、工作分解 WS0~WS8--**实施顺序与任务行以该纪要第六/七节为准**，合并完成后归档）。
> **标注约定**：正文写**目标态**；每节首行【现状】说明代码现在到哪。引用行号动手前须复核未漂移。

---

## 1. 定位、铁律与约束

### 1.1 AI 对话框的定义（01 §7 修订版，2026-08-23）

对话框是**平台内一切操作的统一入口**：查数、写数、结构问答、审计回顾、数据源与知识库管理。对平台外话题（闲聊、天气、写作）一律拒答并引导回平台能力。AI 可调用的能力以注册的 tool 白名单为准，**未注册即禁止**。永不注册：DDL 执行、数据源删除、模型自身配置修改、任意代码执行。

### 1.2 三条铁律（贯穿全篇，违反任何一条都算实现错误）

1. **行数据不出网；工件进出模型上下文恒经脱敏管道**--`privacy_mode` 与行数上限照旧，会话存储/结果工件不是旁路；本地落盘不算出网。
2. **模型可见世界恒代号**--schema、tool 入参回显、tool result 回喂一律代号化（B3）；真实名字只存在于闸门之后与展示层。
3. **技能只收窄不扩权；禁止靠缺席**--技能对工具只做交集过滤，任何组合跑不出工具信任的并集；一个能力"不能做"的最强保证是它的 tool 不存在。

### 1.3 目标（用户可感知的承诺）

* **敢用**：自然语言直达结果；写操作可解释、可确认、可回滚、可审计（`A1/A3/A4/G4`）。
* **敢让 AI 进内网**：行数据不出网，出网的是"结构+聚合+脱敏"，每一笔可枚举（`B1/B2/B3`）。
* **快且稳**：首包 `<1.2s` 感知（preflight 全程 `≤2s`，含降级）；`run_query` 中位 `<300ms`；EXPLAIN 失败不阻塞。

### 1.4 非目标

不做 BI 仪表盘、重型 RAG、跨库联邦、**平台外通用 chatbot**（精确化口径见 §1.1）、星图 2D/3D 切换。新增能力必须同时落到 `Skill.tools[]` 白名单与 §3.1 意图集（封闭、版本化），否则不做；新增意图的准入 = 独立执行路径 + 降级路径 + 01 §9 决策记录。

### 1.5 约束

* **单库**：每请求只连一个 `connection_id`；跨库 JOIN 不支持，报错明示。
* **会话边界 = 连接边界**：切库自动换 session，上下文天然清零（结构性保证，不靠确认流程）。
* **本地可信层**：闸门/脱敏/代号化/清单为本地纯函数，离线可跑、可审计；模型只负责"写"，永远不负责"放行"。
* **单管道**：`context -> 脱敏 -> 清单 -> gateway`，禁止旁路直连模型。preflight、拒答、压缩摘要等**每一次**模型调用都在管道内。

---

## 2. 总览（数据流）

```
用户问题（含追问）
  │
  ├─ preflight（统一 owner，≤2s 预算，一次脱敏/一次清单/一次审计）
  │    关键词快判 ──多数请求 0 次 LLM──▶ 直接出意图
  │    拿不准 ──▶ 一次 LLM 返回 {intent, tags}（拿不准当 query）
  │    超时/异常 ──▶ 降级关键词快判；追问轮：L2 种子并入上轮 card.tables
  │
  ├─ 意图 -> 技能路由（dispatcher 感知 enabled 集合；不可用技能 -> 降级应答给出路）
  │
  ├─ offtopic ──▶ refusal 技能：云端引导式拒答（无工具，strict 降本地固定文案）
  ├─ 数据面（query/report/schema/write/ddl）
  │     └─ 组装上下文（L2 融合，候选子图 ≤20 表）-> loop（有界纠错）-> 闸门 -> 渲染
  └─ 控制面（audit_qa/connection_setup/…，分批）
        └─ 平台工具（trust/confirm/audit 三件套，source=system_tool 审计）
```

**落盘**：`backend/app/ai/loop.py:32 stream` 统一入口，按 preflight 意图分流技能；`frontend AiRail.tsx` 经 SSE 订阅。

---

## 3. Preflight 与意图

> 【现状】`intent.py` 的 `classify_mode`/`classify_tags` 与 `agent/dispatcher.py` 是三次独立 LLM 调用，各自内联脱敏/清单/审计（重复代码）；无 `is_query`。【目标】合并为 `ai/preflight.py` 单模块单调用。

### 3.1 意图集（两平面，v1 封闭集，版本化）

| 平面 | 意图 | 执行路径 | 信任 |
|---|---|---|---|
| 数据面 | `query` | 检索 -> 单条 SQL -> 闸门 -> 卡片 | 闸门 |
| 数据面 | `report` | 章节化剧本（clarify->plan->execute->narrate） | 闸门（只读工具集） |
| 数据面 | `schema` | 直接答结构，**跳过检索管线，不执行 SQL** | 只读结构 |
| 数据面 | `write` | run_dml 预览 -> 确认协议（§6） | 闸门 REVIEW |
| 数据面 | `ddl` | draft_ddl -> 发编辑器，永不执行 | 草案 only |
| 控制面 | `audit` 等 | 平台工具（§4，分批） | 工具 trust |
| 兜底 | `offtopic` | refusal 技能（§3.4） | 无工具 |

单标签 + 降级顺序：能答结构的不猜写、**拿不准当 `query`**（误判代价矩阵：五类数据面意图互错皆为"便宜错误"--loop 模型保有行动能力、闸门兜底；唯一贵的方向是误判成 offtopic，由默认方向堵死）。写意图判定保守："删掉/改成"才算 write，宁可漏进 query。

### 3.2 判定流程

* **关键词快判**：多数请求零 LLM 直出（`SELECT|查|统计|多少|按.*月` -> query；`有哪些表|什么结构` -> schema；`删掉|改成` -> write；平台外特征词 -> offtopic）。
* **LLM 单次调用**（拿不准时）：输入 = 问题脱敏后 + 对话尾部（最后一条 user + 上轮 assistant 结尾，解决 report 澄清阶段裸回答"最近30天"的判类）+ 已确认标签清单；输出 `{intent, tags[]}`；相邻对边界（query/report、query/schema、query/write）以例句写进 prompt。
* **超时预算**：整个 preflight `≤2s`，超时/异常降级关键词快判。
* **反馈闭环**：预测意图 vs 实际执行路径（tool 使用）的不一致率入审计；哪个相邻对错得多就补哪对的例句。

### 3.3 审计

preflight 的 LLM 调用是真实出网：问题脱敏后出网、`build_manifest`（`source=egress-intent`）一条、严格档强制 mock 不出网。**全 preflight 只做一次**脱敏/清单/审计（这是合并三处内联调用的验收点）。

### 3.4 拒答（offtopic -> refusal 技能）

* 模型角色严格限定为"礼貌拒绝 + 引导回平台能力"，prompt 明令禁止回答问题本身、禁止延伸话题；约束为 prompt 级（认账其软性：此路径出网的仅问题文本，行数据红线不涉及，不做输出侧过滤）。
* 引导素材注入连接名 + 已确认领域标签（可复用 C2 建议问题），只注入结构信息。
* 每次调用有 manifest + egress 审计；strict 档降级为本地固定拒答文案（零模型、离线）。

---

## 4. 技能与工具（Skill / Tool 分层）

> 【现状】5 个数据面工具已注册（`tools/registry.py: TOOL_SCHEMAS`）；内置 query/report；`update_skill` 已支持 `enabled`；`skill_tool_schemas` 只做交集（收窄不变式已成立）。平台工具、trust 元数据、新内置技能均未落。

### 4.1 分层原则

* **Tool = 执行单元**：原子、无状态、一个动词对一个对象，**自带信任等级**（SQL 工具=闸门；平台工具=只读/变更/破坏性）。安全执行点永远在 tool 层，不可上移。
* **Skill = 能力单元**：自身不执行，是组合+策略（工具白名单 + 指导书 system_prompt + 剧本 steps + 触发词）。关系多对多。
* 链条：意图（路由）-> 技能（组合与策略）-> 工具（执行与安全）。
* 准入：新意图=独立执行路径+降级路径+决策记录；**新技能≈零安全审查**（只是现有工具的新组合）；**新工具=安全评审**（评审重量压 tool 层）。

### 4.2 工具注册规格（平台工具必须带齐；数据面工具补挂 trust）

```
{ name, schema,                  # 模型可见的调用契约
  trust: 只读 | 变更 | 破坏性,
  confirm: None | 确认卡 | admin,
  audit:  source=system_tool,
  registry: 可被 admin 开关 }
```

**密钥直入 vault**：含密参数（连接串等）本地解析、密钥直入 E1 保险库，模型只见结构参数（host/库名/方言）--秘密不走模型上下文。

### 4.3 工具总表（v1 = 14 个）

| # | tool | 作用 | 信任 | 关键约束 | 批次 |
|---|---|---|---|---|---|
| 1 | `get_schema` | 库结构摘要 | 只读 | 30s 缓存 | 既有 |
| 2 | `describe_table` | 表/列结构 | 只读 | | 既有 |
| 3 | `run_query` | 只读 SQL | 只读·闸门 | LIMIT 注入、include_data 三档、redact_rows | 既有 |
| 4 | `run_dml` | 写 SQL | 变更·闸门 REVIEW | preview/blast/rollback + confirm_token（§6） | 既有 |
| 5 | `draft_ddl` | DDL 草稿 | **永不执行** | 发编辑器，人点执行 | 既有 |
| 6 | `load_result` | 按 result_id 取会话工件 | 只读 | 行数上限 + 恒过脱敏管道；只用于"看形状" | 新增 |
| 7 | `query_audit` | 审计日志查询 | 只读·系统 | 结果回喂过脱敏管道 | 1 |
| 8 | `get_last_operations` | "我刚才干了什么" | 只读·系统 | query_audit 快捷封装 | 1 |
| 9 | `parse_connection` | 解析连接串 | 只读 | 密钥直入 vault，模型只见结构参数 | 2 |
| 10 | `register_connection` | 注册数据源 | 变更 | 确认卡（亮 host/db/方言） | 2 |
| 11 | `test_connection` | 连通性测试 | 只读 | | 2 |
| 12 | `build_knowledge` | 构建 KB | 本地重活 | 进度事件；嵌入指纹触发重嵌 | 2 |
| 13 | `annotate_tag` | KB 标注草案 | 变更 | 走现有 draft->人工确认流 | 3 |
| 14 | `export_result` | 按 result_id 导出 | 变更（写本地） | 轻确认 | 3 |

**永不注册**：删除数据源 · 修改模型配置 · 任意代码执行 · DDL 执行。批次 2/3 视批次 1 上线后的真实使用反馈再排，不提前承诺。

### 4.4 技能总表（v1 = 10 个）

| # | skill | 能力 | 工具集 | 开关 |
|---|---|---|---|---|
| 1 | `query` | 4 步单查询 | get_schema, describe_table, run_query, load_result | **常开（地板）** |
| 2 | `refusal` | 引导式拒答（§3.4） | （无工具） | **常开（地板）** |
| 3 | `report` | 章节化分析报告 | run_query, get_schema, describe_table, load_result | 可关 |
| 4 | `schema` | 结构问答（跳检索、零 SQL） | get_schema, describe_table | 可关 |
| 5 | `write` | 写操作（REVIEW 态势前置） | run_dml, run_query, describe_table, get_schema | 可关 |
| 6 | `ddl` | DDL 草稿 | draft_ddl, get_schema, describe_table | 可关 |
| 7 | `audit_qa` | 审计问答/操作回顾 | query_audit, get_last_operations, load_result | 可关 |
| 8 | `connection_setup` | 数据源接入（贴链接全链路） | parse_connection, register_connection, test_connection, build_knowledge | 可关（默认关） |
| 9 | `kb_annotate` | 知识库标注 | annotate_tag, get_schema | 可关 |
| 10 | `export` | 导出 | export_result, load_result | 可关 |

### 4.5 技能开关（用户可勾选）

* 三层权限：**工具层管信任（不可开关）· 技能层管能力（用户勾选）· 意图层管路由**。勾选只能收窄。
* **地板常开**：`query` + `refusal` 不可关闭（"拿不准当 query"兜底依赖它）。
* **关闭的技能给降级出路**：路由到不可用技能时明确应答"此能力已关闭，可在设置开启"；C2 建议只引用 enabled 技能。
* **团队模式最严者胜**：组织策略 ∩ 个人勾选，成员勾选不能恢复管理员禁用项。
* 勾选变更写审计。

### 4.6 自定义技能

`data_dir/skills.json` / system_db 持久化继续存在，`validate_skill` 白名单校验（写工具禁入只读技能）不变，同受"只收窄"不变式约束。

---

## 5. 会话记忆与结果工件

> 【现状】`state.chats` 已有服务端 session 与消息落库（`api/ai.py: upsert/append_messages`，SSE 流结束后落库），但 **loop 上下文仍从前端 `req.messages` 组装**；无 result_id 工件；无压缩。【目标】服务端接管历史组装。

### 5.1 会话模型

* **记忆随 session 走、服务端落库**；历史组装、压缩策略是服务端职责；前端 `req.messages` 降级为兼容通道。
* **连接边界 = 会话边界**：切库自动换 session，杜绝"切库后模糊指令落在错误的库上"。
* 服务端会话记忆同时喂 preflight（对话尾部判类）与 L2（追问轮种子）。

### 5.2 结果工件与 result_id 寻址

* 每张 `sql_card` 落一个 `result_id`，随会话持久化（列名/行数/capped rows）；与 report 的 `[r id]` 引用体系**统一为一套**。
* id 在对话历史里流转，**引用解析由模型做**（"前面第 2 个结果"= 模型在历史中找到对应 id），不做按位置取的后端机制。
* `load_result` 工具按 id 取工件；**恒过脱敏管道 + 行数上限**（铁律 1）；工件只用于"看形状"（列名/行数/样例），不用于"算总数"。
* 工件落盘本地（chat 存储）不算出网；进出模型上下文必经管道。

### 5.3 上下文压缩（v1 只压缩，不截断）

* **压缩的是叙事，保住的是结构**：卡片骨架（result_id/表名/verdict/rowcount）为不可压缩最小单元（追问路由与引用机制依赖它）。
* **机械优先**：先丢最老轮次、保留骨架；不够再上 LLM 摘要。**LLM 摘要本身是一次出网**（输入为整段对话），必须走单管道 + manifest + egress 审计，不得成为唯一不被枚举的出网。
* **豁免于压缩**：当前轮；未决状态（等确认的 run_dml 预览及其 rollback 附件，要么执行要么显式过期，不能被无声清除）。
* 截断暂不做，待真实会话长度分布出来再定阈值。

### 5.4 上下文相关追问（三路径）

| 类型 | 例 | 路径 |
|---|---|---|
| 改写重查 | "那按周统计呢" | 模型读历史写新 SQL -> 照常过闸门 -> 执行 |
| 工件操作 | "导出前面那个结果" | 不查库，按 result_id 取工件 |
| 数据回加工 | "把刚才两个结果加起来" | 原则引导写新 SQL 让数据库算；工件只看形状 |

* **追问轮的 L2 路由必须吃上下文**：追问句无领域词，候选种子并入被引用轮的 `card.tables` 再走融合扩展。
* **铁律：上下文相关重查无安全捷径**--"再跑一遍"照常过闸门；"把刚才查出来的都删了"是全新 DML 从头走 REVIEW；引用解析在模型层，安全评估在执行层，两层永不短路。

---

## 6. DML 确认协议（chat loop 内）

> 【现状】`run_dml` 有 preview/blast/rollback 卡片；无 confirm_token、无 pending 状态；审批流 E2 批准执行绕过闸门（审计 2026-08-21 实锤）。【目标】三段协议 + 顺修 E2。

```
① 模型调 run_dml -> 闸门 REVIEW -> 算 preview_rows / blast / rollback
     -> loop 本 turn 到此为止（不进下一轮 tool call，防模型幻觉"已执行"）
     -> 下发 sql_card（verdict=review + preview/blast/rollback + confirm_token）
     -> 会话状态 pending_dml = {token, sql, preview, rollback, expires_at}（豁免压缩）
② 用户三选一：
     确认执行 -> POST /query（confirm=true, confirm_token, origin=AI）
     取消     -> 清除 pending_dml，"用户拒绝了"写回历史（模型下轮可见）
     不理     -> 过期清除（10 分钟），同 SQL 需重新走 preview
③ 后端收到确认：
     -> 校验 token（一次性、未过期、SQL 哈希一致 -- 防 TOCTOU）
     -> 重新过闸门（确认只是"人看过预览"的凭证，不是通行证）
     -> 执行 -> 审计带 confirm_token/turn_id，与 ① 的 preview 审计闭环
     -> 结果卡片回到会话（result_id 照常落）
```

团队模式下 ② 换成"admin 批准"，其余相同；审批执行同样必须重新过闸门（E2 修复）。

---

## 7. 检索层（L2：查什么）

> 【现状】标签+向量+FK 2 跳融合已有（`context.py: assemble_context_full`）；候选集无封顶、无纠错回路、无覆盖率度量。

* **双通道融合**：`tag_tables = route_tables(tags)`（只认 confirmed，draft 不影响）`∪ vec_tables = vector_route_tables(q, 6)`（哈希嵌入离线默认，`api` 走 `TABLETALK_EMBEDDING_*`）-> `expand_tables(seeds, 2)` -> **封顶 20 张表**，超限砍表顺序：标签命中 > 向量 > 2 跳外围。封顶理由：loop 有 LLM 兜底（可 get_schema 纠错），选漏的代价大于上下文 token 的代价。
* **summarize 只给 routed 的 DDL**，`kb.to_context` 追加 top-N 文档（列级注释）。
* **有界纠错**：`run_query` 报 no such table/column 时，loop 捕获并在 tool result 注入一次完整表清单，允许模型重写，计入 MAX_TURNS--与 system prompt"一步写出最终查询"并存：禁探索，但给一次有界的自我修正。
* **覆盖率度量**：done 时对比最终 SQL 表集合 vs `candidate_tables`，入 `context_meta` 与审计--检索参数（阈值/top_k/hops/封顶）调优的唯一数据来源，WS1 落地当天即带上。
* **模板复用（C6 修订）**：匹配在**脱敏前的原文**上做（脱敏只管出网）；命中后**确认卡**（亮"问题库命中 + 将执行的 SQL"，用户点执行），不静默执行；保存时只收只读问题；阈值可配 + 误命中反例验收用例。
* **隐私**：`raw_schema -> filter_sensitive -> codify_schema`，KB 文本与 `manifest.tables` 同步代号化；映射 `data_dir/codify-{conn_id}.json:600` 永不出网，展示层 `decodify_text` 还原。

---

## 8. 安全与出网

### 8.1 闸门（`app/safety/` 纯函数，不变部分）

三档：`read(ALLOW) -> DML(REVIEW) -> DDL(AI BLOCK/人 REVIEW)`；`R1~R7` 规则，aggregate 取最严重 verdict；策略覆盖（A2 表级/模式级/阈值级，热更新）；成本防护（A5 方言 EXPLAIN，超阈值升 REVIEW，失败放行 + `cost_degraded` 审计）；只读硬边界（`read_only` 时非 ALLOW 直 BLOCK）；`_auto_cap` 注入 `LIMIT max_rows+1`；大整数转字符串。细节见 02 A 系列，此处不重复。

### 8.2 B3 往返不变式（铁律 2 的落地）

> 【现状】**执行前还原从未实现**（审计 B3#3：tool args 原样过闸门执行，敏感表 AI 查询报 `no such table: t_17`，核心路径断裂）；tool result 回喂真实列名。【目标】：

1. **执行前还原**：tool args 先 `decodify_text` 再过闸门、再执行；
2. **回喂代号化**：tool result 回喂模型前表/列名 codify，模型可见世界恒代号；
3. **审计记真名**：审计记录真实表名，并注明与清单代号的对应关系。

### 8.3 出网清单（B1）与三档模式（B4）

* **每一次**模型调用有 manifest + egress 审计：主 loop、report、**preflight（§3.3）、refusal（§3.4）、LLM 压缩摘要（§5.3）**--清单口径不含例外。
* 三档：strict（强制 mock、include_data 一律拦、完全离线）/ standard（结构+脱敏聚合）/ open（明文，逐查询授权 P2 再做）；`manifest.tables == candidate_tables` 一致性校验不变。
* 问题库命中分支合成 `provider=local` 清单（零模型调用也如实枚举）。

---

## 9. 结果渲染与 SSE 契约

### 9.1 SSE 事件契约表（顺序不变式：`turn_start` 恒首、`manifest` 先于任何 `text`、`done` 恒尾）

| 事件 | 载荷 | 驱动前端 |
|---|---|---|
| `turn_start` | `{connection}` | 轮次开始 |
| `stage` | `{stage: intent\|retrieval, value/tags, tables, vec_tables}` | 4 步的"意图/表检索"步骤（真实数据，禁假定时器） |
| `manifest` | `{manifest}` | ManifestView（redactions 回填） |
| `think` | `{text}` | 4 步的过程行 / tool 调用行 |
| `text` | `{content}` | 流式回答（B3 已还原展示） |
| `sql_card` | `{card: verdict/sql/result/blast/rollback/confirm_token/question_library…}` | SQL 卡 / 确认卡 / 命中卡 |
| `gate` | `{verdict, reasons, preview_rows}` | 4 步的"闸门"步骤 |
| `report_start/plan/section/narration/report_done` | 见 report 剧本 | 报告视图 |
| `error` | `{code?, message}` | toast |
| `done` | `{}` | 轮次结束（含 `context_meta.coverage` 随审计落盘） |

### 9.2 4 步思考流（真驱动）

`AiRail ThinkPanel` 默认展开；4 段 `intent/retrieval/sql/gate` 由真实 `stage/think/sql_card/gate` 事件驱动；`intent/retrieval` 的 detail = `context_meta` 的 tags 与 candidate_tables/vec_tables。**移除 `playSteps` 1500ms 假定时器**（v0.1 草稿问题 3 的整改）。拒答轮不显示 4 步。

### 9.3 渲染对照

`manifest`->ManifestView · `blast`->BlastView · `rollback`->RollbackView · `report`->ReportCard（`[r id]` 回跳）· 审计逐条 `rule_id`。C4/C5/C6 现行规范不变（见 03 §3）。

---

## 10. 失败与回退

* **preflight**：LLM 超时/异常 -> 关键词快判（方向仍是拿不准当 query）；strict 强制 mock。
* **网关**：`httpx` 超时/5xx -> `provider.chat` 抛，SSE `error` 事件；`cloud+无 key` 显式报错（债 #2），不再静默降 mock（`/ai/test` 除外）。
* **闸门**：parse_failure -> REVIEW（never ALLOW）；`preview_rows` 3s 超时 None。
* **确认协议**：token 过期/不匹配 -> 确认失败，重新走 preview；审批链同理。
* **检索**：候选为空 -> 如实空态（结构恒可见，零定制）；纠错一次仍失败 -> 模型如实告知。
* **报告**：plan 空 -> error；section 非 ALLOW -> `ok:false` 卡不阻断后续章；narration 失败 -> 失败文案，不回填 mock。
* **KB 冷启动**：`/ai/chat` 现状 `kb_status != ready` 直接拒绝（AI 被关死）；【目标】后台构建 + 进度事件，或首 turn 降级 schema-only 上下文（待确认项，见纪要）。

---

## 11. 可观测与审计

* **出网可枚举率 100%**：chat / report / preflight / refusal / 压缩摘要 / 问题库（provider=local）全部有清单与审计。
* **审计 JSONL**（`schema_version:1`）：`connection/origin/tier/verdict/status/sql/elapsed_ms/reasons/tables/report_id/approval_id/confirm_token/rollback_ref/manifest/estimated_rows/cost_degraded/context_meta.coverage`；block/review 恒非空 reasons（logger 兜底）。
* **系统操作**：平台工具调用 `source=system_tool` 审计；技能开关变更写审计。
* **意图质量**：预测意图 vs 实际路径不一致率、检索覆盖率均入审计，作为参数调优与北极星指标的数据源。

---

## 12. 测试与验收（门控：`pytest -q` + `typecheck` + `build`，交互改动加 e2e）

* **单元**：preflight 六类意图例句/超时降级/兜底方向；confirm_token 状态机（TOCTOU/过期/一次性）；压缩骨架保全与 pending 豁免；候选封顶与砍表顺序；B3 执行前还原与回喂代号化；codify/redact 既有用例族。
* **集成**：egress 一致性（manifest.tables == candidate_tables，含 preflight/refusal/压缩）；strict 全链离线（preflight 不出网）；会话工件按 id 取回 + 回喂过管道；DML 确认审计闭环（preview <-> execute 关联）；追问轮候选种子。
* **攻击**：`gate/cases.yaml` 100 条 BLOCK 基准不回退。
* **前端**：4 步真驱动 e2e；确认卡三态路径；技能关闭降级文案。

---

## 13. 实施对照

实施文档：`docs/implementation/ai-planning-execution.md`（现状差距、任务卡 WS1~WS8、验收标准、实施顺序--**实施以该文档的任务卡为单位**）。设计评审决策记录（D1~D15）归档于 `docs/pending-decisions/2026-08-22-ai-planning-execution.md`，实施不必读。

---

## 14. 关键文件索引（改前必读）

`backend/app/ai/preflight.py`【目标，新】 · `intent.py`【现状，将大幅缩减】 · `agent/dispatcher.py` · `context.py: assemble_context_full` · `loop.py:32 stream / 76 chat_stream` · `report.py: report_stream` · `skills/registry.py / builtin/` · `tools/registry.py / sql.py` · `safety/gate.py / rules.py / redact.py / codify.py` · `core/query.py / questions.py` · `state.py: chats` · `api/ai.py / query.py` · `frontend/src/renderer/src/components/AiRail.tsx`

---

> **审阅点**（v0.2 待用户最终确认后生效）：意图集封闭性、D15 批次承诺（只锁批次 1）、压缩 v1 不截断、`connection_setup` 默认关。确认后按纪要 WS0~WS8 逐项 TDD 落盘。
