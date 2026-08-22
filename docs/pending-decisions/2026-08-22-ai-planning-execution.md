# AI 规划与执行 · 设计讨论纪要（待确认方案）

> 日期：2026-08-22（2026-08-23 增补实施部分） · 参与方：产品负责人 × Claude
> 性质：对 `docs/product-handbook/08-ai-planning-execution.md`（v0.1 草稿）的评审讨论记录 + **实施指导**（第五~七节）。
> **地位**：本文件不是第二事实源。其中的"已确认决策"需合并进 08（升 v0.2）并同步 01 §7 / 02 后，方可指导实现；合并前以 product-handbook 现文为准。
> 状态：**已定稿归档（2026-08-23）**。决策（D1~D15）已合并进 product-handbook 08 v0.2 / 01 §7 / 02 G 系列 / 03；第五~七节的实施内容已整合并取代为 `docs/implementation/ai-planning-execution.md`（实施以那份文档为准，本文件仅作决策推理过程存档，实施者不必读）。

---

## 一、对 08 v0.1 草稿的设计问题清单（评审结论）

评审对照了 `intent.py` / `dispatcher.py` / `loop.py` 现状与 `audit-2026-08-21-implementation-review.md`，识别出 6 个实质设计缺陷 + 一批一致性问题：

| # | 问题 | 要点 |
|---|---|---|
| 1 | **前置决策链无归属无预算** | L1 / dispatcher / L2 三次串行 LLM 预调用（云模型下 1.5~2.4s），与 `<1.2s` 首包目标冲突；三次调用各自内联脱敏/清单/审计，无统一 owner |
| 2 | **L1 误判方向反了** | 草稿"拿不准回 chat"，但 query 误判成 chat 是不可恢复的死胡同（chat 无工具）；应"拿不准当 query" |
| 3 | **模板复用（C6）的信任缺口** | ①相似度 ≥60 模糊命中即静默执行；②匹配发生在脱敏之后（`loop.py:108`，含敏感串的问题永远命不中）；③DML 问题命中后走 `assess_sql` 非 ALLOW 即弃，永远无法命中；④阈值无验收口径 |
| 4 | **L2 融合只有选表逻辑** | 候选集无上限（大 schema 上 2 跳扩展可扫进半张图）；无 context token 预算；标签/向量冲突无仲裁；检索错了无纠错回路（与"一步写出最终查询"的 prompt 自相矛盾）；无检索质量指标（阈值/参数无数据支撑） |
| 5 | **B3 代号化无往返不变式** | ①§7.1 声称 `_decodify_sql` 已落，与审计 v2 结论相反（执行前还原从未实现，B3 核心路径断裂）；②模型活在代号世界（schema=t_17/c_3）但 tool result 回喂真实列名，模型下一轮会混用两套名字 |
| 6 | **DML 确认协议缺失** | `run_dml` needs_confirm 后 loop 终止还是继续、UI 确认走哪条通路、确认执行的审计如何与 preview 关联，均未设计（**此项讨论中未展开，仍待定**） |

一致性问题（修文档即可）：草稿"已落"声明与事实不符（`_decodify_sql`、`Tier` 导入两处与审计结论相反）→ 建议逐句标 `【现状】/【目标】`；§3 与 §7.3 的 chat 清单口径互相矛盾；L1 关键词表过脆（"看"）；SSE 事件契约未成表；KB 冷启动（首次懒构建秒级阻塞首包）不在性能预算内。

---

## 二、已达成的决策（按讨论顺序）

### D1 · AI 对话框的产品定义：平台内的全能助手

- **定义**：对话框是平台内一切操作的统一入口；平台外话题拒答引导。
- 这与 01 §7"不做通用 chatbot"不冲突，但需精确化并记 01 §9 决策日志："允许对话式引导与平台内操作，禁止回答平台外问题本身"。
- 依据：harness 模式（Claude Code / Cursor 等）已验证"一个对话入口 + 受权限约束的工具集"的模式；与产品第一性原则同构（模型只提议，本地层放行）。
- 所有请求直发云端大模型（无本地模型部署；strict 档除外，见 D8）。

### D2 · 拒答（offtopic）形态：云端生成的引导式拒答

- 模型角色严格限定为"礼貌拒绝 + 引导回平台能力"，禁止回答问题本身、禁止延伸话题。
- 拒答 prompt 注入连接名、已确认领域标签（可复用 C2 建议问题）做**具体**引导；仍只注入结构信息，符合隐私红线。
- 约束靠 system prompt，**认账其软性**：此路径出网的仅问题文本（query 路径本来也出网），行数据红线不涉及，故不做输出侧过滤；此取舍写进 08。
- 出网清单照样要有（manifest + egress 审计）；strict 档降级为本地固定拒答文案。

### D3 · Preflight 合并：一次调用、统一 owner

- L1 判定 + L2 标签分类 + 技能路由合并为**一次 LLM 调用**，返回 `{intent, tags[]}`；拿不准的字段才允许补第二次。
- 脱敏一次、manifest 一次、审计一次；给整个 preflight 设超时预算（提议 2s），超时/异常降级关键词快判。
- 输入看**对话尾部**（最后一条 user + 上轮 assistant 结尾），解决 report 澄清阶段裸回答（"最近30天"）的判类问题。

### D4 · 误判兜底方向：拿不准当 query

- 代价矩阵论证：六类意图互相误判多为"便宜错误"（loop 模型保有行动能力、闸门兜底，损失效率不损失正确性）；唯一贵的方向是误判成 offtopic（无工具死胡同），由默认方向堵死。
- 配套保险：①相邻对边界（query/report、query/schema、query/write）显式定义进 prompt + 例句；②预测意图 vs 实际路径的不一致率入审计，按错得多的一对补例句；③UI 显式选择永远压过分类器。

### D5 · 意图分类：两个平面，封闭集，版本化

**数据面（五类）+ 控制面（分批增加）+ offtopic 兜底**：

| 平面 | 意图 | 执行路径 | 信任等级 |
|---|---|---|---|
| 数据面 | `query` | 检索 → 单条 SQL → 闸门 → 卡片 | 闸门 |
| 数据面 | `report` | 章节化剧本 | 闸门（只读工具集） |
| 数据面 | `schema` | 直接答结构，**不执行 SQL、跳过检索管线** | 只读结构 |
| 数据面 | `write` | run_dml 预览 → 确认流 | 闸门 REVIEW |
| 数据面 | `ddl` | draft_ddl → 发编辑器，永不执行 | 草案 only |
| 控制面 | `audit`（第一批） | 查审计日志/操作回顾 | 只读系统操作 |
| 控制面 | `kb` / `export` / `connection`（后续分批） | 见下"待确认" | 按工具信任元数据 |
| 兜底 | `offtopic` | 云端引导式拒答（D2） | 无工具 |

- 单标签 + 降级顺序：能答结构的不猜写、拿不准当 query；写意图判定保守（"删掉/改成"才算，宁可漏进 query）。
- 意图集**封闭、带版本号**；新增准入标准：独立执行路径 + 降级路径 + 决策记录。
- **闸门管不了控制面**：控制面操作不产生 SQL，需第二套信任层（工具信任元数据：只读/变更/admin + 各自确认流）；原则不变--模型可提议任何操作，本地层按各自规则放行。
- **防暗坑**：对话中切换连接 = 最重确认级或 v1 禁止（见 D6，最终由会话边界解决）。
- 控制面分批原则："读优先、写靠后、碰凭证最后"。第一批建议 `audit`（零风险纯收益，素材在本地 audit.log）。

### D6 · 会话记忆：服务端 session，连接边界 = 会话边界

- 记忆随 session 走、服务端落库（chat.db），不再靠前端带全量历史；历史组装、压缩策略变成服务端职责。
- **切库自动换 session**：上下文天然清零，结构性解决"切库后模糊指令落在错误的库"的暗坑（结构保证优于流程保证）。
- **结果工件 result_id 寻址**：每张 sql_card 落一个 `result_id`，随会话持久化；id 在对话历史里流转，模型在历史中自己找到引用目标（"前面第 2 个结果"由模型读历史解析，非后端数数）。与 report 的 `[r id]` 体系**统一为一套**。
- ~~get_last_result~~ 方案否决：按位置取的工具无泛化能力（前 2 个/前 3 个），id 寻址才是通解。

### D7 · 上下文压缩策略（v1 只做压缩，不做截断）

- 压缩单位：**压缩的是叙事，保住的是结构**--卡片骨架（result_id / 表名 / verdict / rowcount）为不可压缩的最小单元，否则引用机制与追问路由被压缩吃掉。
- **LLM 摘要压缩本身是一次出网**（输入为整段对话），必须走单管道（脱敏进、manifest + egress 审计照记），不得成为唯一不被枚举的出网；机械压缩（丢轮次/保骨架）优先，不够再上 LLM 摘要。
- **豁免于压缩**：当前轮、未决状态（等确认的 run_dml 预览及其 rollback 附件要么执行要么显式过期，不能被无声清除）。
- 截断暂不做，待真实会话长度分布出来再定阈值。

### D8 · 上下文相关追问的处理

- **引用解析由模型做**（历史在上下文里），无后端确定性"定位→改写"循环；"循环"即现有 function-calling loop。
- 追问分三类，路径不同：
  1. **改写重查型**（"按周统计呢"）：模型写新 SQL → 照常过闸门 → 执行；
  2. **工件操作型**（"导出前面那个结果"）：不查库，按 result_id 取已持久化结果；
  3. **数据回加工型**（"把刚才两个结果加起来"）：原则引导模型**写新 SQL 让数据库算**，工件进上下文只用于"看形状"（列名/行数/样例），不用于"算总数"（数字正确性 + 少一次行数据进上下文）。
- **追问轮的 L2 路由必须吃上下文**：追问句无领域词，候选种子并入被引用轮的 `card.tables`，再走正常融合扩展（依赖 D7 的骨架保全）。
- **铁律：上下文相关重查无安全捷径**。"再跑一遍"照常过闸门；"把刚才查出来的都删了"是全新 DML 从头走 REVIEW；引用解析在模型层，安全评估在执行层，两层永不短路。

### D9 · 出网与工件的统一管道

- **工件进出模型上下文恒经同一脱敏管道**（privacy_mode / 行数上限照旧），会话存储不得成为绕过脱敏的旁路。
- 工件本身落盘本地（chat.db）不是出网，不受此管。
- B3 往返不变式：**模型可见世界恒为代号**（schema、tool 入参回显、tool result 回喂一律代号化）；真实名字只存在于闸门之后与展示层；审计记录真名并注明与清单代号的对应。执行前还原（`_decodify_sql`）现状未实现，08 需改为【目标】。

### D10 · 检索层补强（对草稿问题 4 的处置）

- 候选集封顶 **20 张表**（放宽自 12，理由：loop 中 LLM 兜底，选漏的代价大于上下文 token 的代价）；超限砍表顺序：标签命中 > 向量 > 2 跳外围。
- loop 保留**有界纠错**：run_query 报 no such table/column 时注入一次完整表清单，允许模型重写，计入 MAX_TURNS。
- done 时记录**候选覆盖率**（最终 SQL 表集合 vs candidate_tables）入审计/`context_meta`，作为检索质量指标，喂参数调优与北极星指标。
- 模板命中（对草稿问题 3 的处置）：**加用户确认步**（亮出"问题库命中 + 将执行的 SQL"确认后执行）；匹配在**脱敏前的原文**上做（脱敏只管出网）；保存时仅收只读问题，DML 命中留后续。

### D11 · DML 确认协议（chat loop 内，用户已认可"逻辑基本自洽"）

三段状态图：

```
① 模型调 run_dml -> 闸门 REVIEW -> 算 preview_rows / blast / rollback
     -> loop 本 turn 到此为止（不进下一轮 tool call，防模型幻觉"已执行"）
     -> 下发 sql_card（verdict=review + preview/blast/rollback + confirm_token）
     -> 会话状态 pending_dml = {token, sql, preview, rollback, expires_at}（豁免压缩，D7）
② 用户三选一：
     确认执行 -> POST /query（confirm=true, confirm_token, origin=AI）
     取消     -> 本地清除 pending_dml，"用户拒绝了"写回历史（模型下轮可见）
     不理     -> 过期清除（提议 10 分钟），同 SQL 需重新走 preview
③ 后端收到确认：
     -> 校验 token（一次性、未过期、SQL 哈希一致 -- 防 TOCTOU）
     -> 重新过闸门（确认只是"人看过预览"的凭证，不是通行证）
     -> 执行 -> 审计带 confirm_token/turn_id，与 ① 的 preview 审计闭环
     -> 结果卡片回到会话（result_id 照常落，可被追问引用）
```

要点：① REVIEW 处终止 loop，"执行了吗"完全交给 UI 卡片；② confirm_token 绑定 SQL 哈希 + 一次性消费（与审批流 E2 的 TOCTOU 同类问题同一修法：审批执行同样必须重新过闸门）；③ 拒绝信号写回历史。团队模式下 ② 的"用户确认"换成"admin 批准"，其余相同。

### D12 · Skill / Tool 分层原则

- **Tool = 执行单元**：原子、无状态、一个动词对一个对象，**自带信任等级**（SQL 工具=闸门；平台工具=只读/变更/破坏性元数据）。安全执行点永远在 tool 层，不可上移。
- **Skill = 能力单元**：自身不执行，是组合+策略（工具白名单 + 指导书 system_prompt + 剧本 steps + 触发词）。
- 关系**多对多**（工具是共享词汇表，技能是任务组织方式）。
- **不变式：技能只能收窄、不能扩权**（过滤只做交集；任何技能组合跑不出工具信任的并集）。评审重量压 tool 层，skill 层保持轻：新增技能≈零安全审查，新增工具=安全评审。
- 链条：意图（路由）-> 技能（组合与策略）-> 工具（执行与安全）。
- 准入测试各归各位：新意图=独立执行路径+降级路径+决策记录；新技能=现有工具的新组合；新工具=原子执行单元+信任等级+安全评审。

### D13 · 平台功能注册制（控制面落地方式）

- **只有被注册成 tool 的平台能力才存在；禁止靠缺席，不靠约束**（与"无 DDL 执行工具"第一不变式同构：模型没有的动词就永远说不出那个句子）。
- 注册规格五件套 + 开关：`{name, schema, trust(只读|变更|破坏性), confirm(None|确认卡|admin), audit(source=system_tool), admin 可开关}`。
- **密钥直入 vault**：含密参数（连接串等）本地解析、密钥直入 E1 保险库，模型只见结构参数（host/库名/方言）--秘密不走模型上下文，与 D9 同一原理。
- 永不注册清单：删除数据源、修改模型配置（自指风险）、任意代码执行、DDL 执行。

### D14 · 用户技能开关（设置页）

- 三层权限拼图：**工具层管信任（安全不变式，任何人不可开关）· 技能层管能力（用户勾选）· 意图层管路由**。
- **地板常开**：`query` + 拒答不可关闭（D4 兜底方向"拿不准当 query"依赖它）。
- **关闭的技能给降级出路，不产生死胡同**：路由到不可用技能时明确应答"此能力已关闭，可在设置开启"；dispatcher 感知 enabled 集合；C2 建议只引用开启的技能。
- **团队模式最严者胜**：组织策略 ∩ 个人勾选，绝不允许个人勾选恢复管理员禁用的能力（防提权后门）。单人模式两档合一。
- 勾选变更写审计（与隐私档位切换同口径）。

### D15 · Tool / Skill 注册总表（v0.1，待用户确认批次）

**Tool × 14**（执行单元，各带信任等级）：

| # | tool | 作用 | 信任 | 关键约束 | 批次 |
|---|---|---|---|---|---|
| 1 | `get_schema` | 库结构摘要 | 只读 | 30s 缓存 | 既有 |
| 2 | `describe_table` | 表/列结构 | 只读 | | 既有 |
| 3 | `run_query` | 只读 SQL | 只读·闸门 | LIMIT 注入、include_data 三档、redact_rows | 既有 |
| 4 | `run_dml` | 写 SQL | 变更·闸门 REVIEW | preview/blast/rollback + confirm_token（D11） | 既有 |
| 5 | `draft_ddl` | DDL 草稿 | **永不执行** | 发编辑器，人点执行 | 既有 |
| 6 | `load_result` | 按 result_id 取会话工件 | 只读 | 行数上限 + 恒过脱敏管道（D9）；只用于"看形状" | 新增 |
| 7 | `query_audit` | 审计日志查询 | 只读·系统 | 结果回喂过脱敏管道 | 1 |
| 8 | `get_last_operations` | "我刚才干了什么" | 只读·系统 | query_audit 的快捷封装 | 1 |
| 9 | `parse_connection` | 解析连接串 | 只读 | 密钥直入 vault，模型只见结构参数（D13） | 2 |
| 10 | `register_connection` | 注册数据源 | 变更 | 确认卡（亮 host/db/方言） | 2 |
| 11 | `test_connection` | 连通性测试 | 只读 | | 2 |
| 12 | `build_knowledge` | 构建 KB | 本地重活 | 进度事件；嵌入指纹触发重嵌 | 2 |
| 13 | `annotate_tag` | KB 标注草案 | 变更 | 走现有 draft->人工确认流 | 3 |
| 14 | `export_result` | 按 result_id 导出 | 变更（写本地） | 轻确认 | 3 |

**永不注册**：删除数据源 · 修改模型配置 · 任意代码执行 · DDL 执行。

**Skill × 10**（能力单元，用户可勾选；信任由所含工具决定）：

| # | skill | 能力 | 工具集 | 开关 |
|---|---|---|---|---|
| 1 | `query` | 4 步单查询 | get_schema, describe_table, run_query, load_result | **常开（地板）** |
| 2 | `refusal` | 引导式拒答（D2） | （无工具） | **常开（地板）** |
| 3 | `report` | 章节化分析报告 | run_query, get_schema, describe_table, load_result | 可关 |
| 4 | `schema` | 结构问答（跳检索、零 SQL） | get_schema, describe_table | 可关 |
| 5 | `write` | 写操作（REVIEW 态势前置） | run_dml, run_query, describe_table, get_schema | 可关 |
| 6 | `ddl` | DDL 草稿 | draft_ddl, get_schema, describe_table | 可关 |
| 7 | `audit_qa` | 审计问答/操作回顾 | query_audit, get_last_operations, load_result | 可关 |
| 8 | `connection_setup` | 数据源接入（贴链接全链路） | parse_connection, register_connection, test_connection, build_knowledge | 可关（建议默认关） |
| 9 | `kb_annotate` | 知识库标注 | annotate_tag, get_schema | 可关 |
| 10 | `export` | 导出 | export_result, load_result | 可关 |

- 意图->技能 1:1（D5 的六类数据面意图各对一个技能，控制面意图对 7~10）。
- 自定义技能（`data_dir/skills.json`）继续存在，同样受白名单校验与"只收窄"不变式约束。
- 注：`load_result` 在多个技能间共享，是 D6 工件寻址的执行载体。

---

## 三、待确认项（下次讨论入口）

1. **D15 总表与批次确认**：14 tool / 10 skill 的清单、批次切分（audit 先行、connection_setup 默认关等）待用户最终拍板。
2. **01 §7 修订文案与决策日志条目**：按 D1 口径起草，待确认后落 01 §9。
3. **08 v0.2 修订稿**：将 D1~D15 合并进 08，逐句标注【现状】/【目标】，修正两处与事实不符的"已落"声明（`_decodify_sql`、`Tier` 导入）；Skill/Tool 总表（D15）落为 08 §6 的新版表格。
4. **preflight 超时预算具体值**（暂提议 2s）与关键词快判的覆盖策略。
5. **模板误命中的验收口径**：什么样的相似问题对*必须不*误命中，需具体用例。
6. 草稿遗留小项：SSE 事件契约表（类型 × 载荷 × 顺序不变式，如 manifest 先于任何 text）、KB 冷启动（后台构建 + 进度事件，或首 turn 降级 schema-only）、chat 清单口径统一（§3 vs §7.3）。

> 已解决并在本轮讨论中确认的：DML 确认协议（原待确认 #2 -> D11）、skill/tool 分层与平台注册制（原 #1 的框架部分 -> D12/D13/D14/D15）。

---

## 四、合并路径

本纪要 → 用户确认待确认项 → 起草 08 v0.2（含现状/目标标注）→ 同步修订 01 §7/§9、02（C6 确认步、控制面条目）、03（4 步 UI 与确认流）→ 按手册 TDD 循环落码。合并完成后本文件可归档删除，避免第二事实源。

---

## 五、现状差距盘点（2026-08-23 核对源码后）

> 逐模块对照"目标（D1~D15）"与"代码现状"。已核实项标注源码位置；实施者动手前请再确认行号未漂移。

| 模块 | 现状（已核实） | 差距（要做什么） | 类型 |
|---|---|---|---|
| 意图层 | `intent.py` 的 `classify_mode`/`classify_tags` 各自内联 manifest/脱敏/审计（大量重复 try/except）；`agent/dispatcher.py` 独立第三次 LLM 调用；无 `is_query` | 统一 preflight：一次 LLM 返回 `{intent, tags}`，一次脱敏/清单/审计，2s 超时降关键词（D3/D5） | 改造 |
| 技能/工具 | 5 个数据面工具已注册（`tools/registry.py: TOOL_SCHEMAS`）；内置 query/report；`update_skill` 已支持 `enabled` 开关；`skill_tool_schemas` 只做交集（收窄不变式已成立） | 新增 refusal/schema/write/ddl 内置技能；工具注册规格扩展 trust/confirm/audit；平台工具 8 个（D12/D13/D15） | 新增+扩展 |
| 会话记忆 | `state.chats` 已有服务端 session（`api/ai.py: upsert/append_messages`，消息在 SSE 流结束后落库）；**但 loop 上下文仍从前端 `req.messages` 组装**；无 result_id 工件；无压缩 | loop 改为从 session store 读历史；`result_id` 工件落库与寻址；压缩策略（骨架保全，D6/D7/D8） | 改造+新增 |
| DML 确认 | `run_dml` 有 preview/blast/rollback 卡片；无 confirm_token；无 pending 状态；**审批流 E2 批准执行绕过闸门（审计 v2 实锤的高优 bug）** | 三段确认协议 + TOCTOU 校验；顺修 E2（D11） | 新增+修 bug |
| 检索层 | 标签+向量+FK 2 跳融合已有（`context.py: assemble_context_full`）；候选集**无封顶**；无纠错回路；无覆盖率度量 | 封顶 20 + 砍表顺序；有界纠错；覆盖率入审计（D10） | 改造 |
| 问题库 | `questions.py: match` 阈值 60（首尾 6 字符启发）；**`loop.py` 在脱敏之后才匹配**；无确认步；保存无只读校验 | 匹配移到脱敏前；确认卡；保存只收只读；误命中验收用例（D10） | 改造 |
| B3 代号化 | codify/decodify 引擎完备；**执行前不还原 tool args**（审计 B3#3，核心路径断）；tool result 回喂真实列名 | 执行前 decodify；回喂 codify；往返不变式（D9） | 修 bug |
| 平台工具 | 无 | 批次 1：`query_audit`/`get_last_operations` + audit_qa 技能；系统操作审计 `source=system_tool`（D13/D15） | 新增 |
| 技能开关 | settings 抽屉已有 skills 分节，`enabled` 持久化在 | 地板常开逻辑、降级文案、勾选写审计、团队模式交集（D14） | 扩展 |
| 前端 4 步 | `AiRail` ThinkPanel 默认折叠 + `playSteps` 1500ms 假定时器（草稿问题 3） | 真事件驱动、默认展开、与 `context_meta` 对齐 | 改造 |
| 其他现状提醒 | `/ai/chat` 有 `kb_status != ready` 即拒绝的闸（KB 冷启动直接把 AI 关死，见待确认 #6）；`/ai/selection` 是硬编码文案（审计 B1#6） | 冷启动体验与 selection 恢复按待确认项处理 | 待设计 |

---

## 六、工作分解（WS0~WS8）

> 每个任务给出：对应决策、涉及文件、验收标准。**验收即结项**（手册 README C-8）；全部任务 TDD，门控 `pytest -q` + `typecheck` + `build`，前端交互改动加 e2e。

### WS0 · 文档先行（手册硬规则 C-1：先补条目再实现）

| 任务 | 涉及 | 验收 |
|---|---|---|
| 08 升 v0.2：合并 D1~D15，逐句标注【现状】/【目标】，修正两处失实声明（`_decodify_sql`、`Tier` 导入） | 08 | 用户评审通过 |
| 01 §7 修订（"对话框是平台内操作统一入口，平台外拒答"）+ §9 决策日志条目 | 01 | 决策日志追加完成 |
| 02 补条目：preflight/意图分层、refusal、会话工件与压缩、DML 确认协议、检索封顶与纠错、B3 闭环、平台工具与技能开关、问题库确认步（各含验收标准） | 02 | 评审通过，任务可在 02 找到出处 |
| 03 补交互：真 4 步、DML/问题库确认卡、技能开关 UI | 03 | 同上 |

### WS1 · Preflight 统一意图层（D3/D4/D5；地基，最先做）

| 任务 | 涉及 | 验收 |
|---|---|---|
| 新建 `ai/preflight.py`：关键词快判 -> 一次 LLM 返回 `{intent, tags}` -> 超时(2s)/异常降关键词；脱敏/manifest/审计在 preflight 内各做一次 | 新 `ai/preflight.py`，收敛 `intent.py`、`agent/dispatcher.py`、`loop.py: stream` | 单测：六类意图各≥2 例句、超时降级路径、"拿不准当 query"、egress 审计恰一条（含 intent 清单） |
| 意图->技能映射表 + dispatcher 感知 enabled 集合，不可用技能给降级应答（"此能力已关闭，可在设置开启"） | `agent/dispatcher.py`、`skills/registry.py` | 单测：关闭 write 技能后说"删掉测试订单"得到降级文案而非死胡同 |
| 追问轮路由：L2 种子并入上轮 `card.tables`（过渡期可从前端 `req.messages` 取，WS3 完成后改从 session store 取） | `preflight.py`、`context.py` | 单测："那按周统计呢"的候选集包含上轮表 |
| 删除三处内联的 manifest/redact/audit 重复代码 | `intent.py`（大幅缩减） | egress 一致性测试仍全绿；`intent.py` 无内联审计代码 |

### WS2 · 新内置技能（D2/D5/D15；依赖 WS1）

| 任务 | 涉及 | 验收 |
|---|---|---|
| `refusal` 技能：无工具 + 引导 prompt（注入连接名/已确认标签）；strict 降级本地固定文案 | `skills/builtin/` | e2e："天气"得到拒答+引导；strict 下离线拒答；每次调用有 manifest+egress |
| `schema` 技能：跳过检索管线直接答结构，不执行 SQL | `skills/builtin/`、`preflight.py` | 单测：意图判 schema 时零向量召回零 SQL 卡 |
| `write`/`ddl` 技能态势（写/结构变更的 UI 预告）；工具集按 D15 表 | `skills/builtin/` | D15 表与 `skill_tool_schemas` 输出一致（单测遍历断言） |
| C2 建议列表只引用 enabled 技能 | 前端 `AiRail.tsx` | e2e：关闭技能后建议不含其引用 |

### WS3 · 服务端会话与工件寻址（D6/D7/D8；最大改造，可与 WS5/WS6 并行）

| 任务 | 涉及 | 验收 |
|---|---|---|
| loop 历史改从 session store 读取（`req.messages` 降级为兼容通道），服务端负责历史组装 | `loop.py`、`state.chats`、`dto.py` | 集成测试：同 session 连续两问，第二问上下文来自服务端 |
| `result_id` 工件：sql_card 落 id，结果（列名/行数/capped rows）持久化到 session 存储 | `chats` 存储、`loop.py` | 单测：两轮查询后工件可按 id 取回 |
| `load_result` 工具：按 id 取工件，**恒过脱敏管道 + 行数上限**（D9） | 新 tool + `tools/registry.py` | 单测：standard 档工件回喂被 redact；strict 拦截行数据 |
| 压缩 v1：机械优先（丢轮次/保骨架），卡片骨架（id/表名/verdict/rowcount）不可压缩；LLM 摘要走单管道+egress；当前轮与 pending 豁免 | 新 `ai/compress.py` | 单测：压缩后骨架仍含全部 result_id；LLM 摘要调用有 egress 审计 |
| 切库自动换 session（连接边界=会话边界） | 前端会话切换 | e2e：切库后追问不带旧库上下文 |

### WS4 · DML 确认协议（D11；依赖 WS3 的 session 存储落 pending 状态）

| 任务 | 涉及 | 验收 |
|---|---|---|
| confirm_token 状态机：生成（绑 SQL 哈希）/一次性消费/过期(10min)；pending_dml 存 session（豁免压缩） | `safety/` 或 `ai/tools/sql.py`、session 存储 | 单测：TOCTOU 用例（确认时换 SQL 被拒）；过期重走 preview |
| loop 在 REVIEW 处终止 turn（不进下一轮 tool call），下发含 token 的确认卡 | `loop.py`、前端确认卡 | e2e：run_dml 后模型不再有后续轮次 |
| 确认通路：`POST /query confirm=true + token + origin=AI`，执行前**重新过闸门**，审计带 confirm_token/turn_id 与 preview 闭环 | `api/query.py` | 集成测试：确认执行的审计条目能关联到 preview 条目 |
| 取消/拒绝写回会话历史（模型下轮可见） | `loop.py` | 单测：拒绝后下轮模型上下文含拒绝信号 |
| **顺修 E2**：审批执行重新 `assess_sql` + read_only 检查（审计 v2 高优 bug） | `api/approvals.py` | 单测：批准无 WHERE UPDATE 仍被闸门拦 |

### WS5 · 检索层与问题库补强（D10；独立，可先行）

| 任务 | 涉及 | 验收 |
|---|---|---|
| 候选集封顶 20，砍表顺序：标签命中 > 向量 > 2 跳外围 | `context.py`/`knowledge/store.py` | 单测：构造大候选集断言封顶与优先级 |
| 有界纠错：run_query 报 no such table/column 时注入完整表清单一次，计入 MAX_TURNS | `tools/sql.py` 或 `loop.py` | 单测：错表 SQL 后模型可一次重写成功 |
| done 时覆盖率（最终 SQL 表 vs candidate_tables）入 `context_meta` + 审计 | `loop.py` | 集成测试：审计条目含 coverage 字段 |
| 问题库：匹配移到脱敏**前**（修 `loop.py:108` 顺序）；命中改确认卡（亮 SQL，用户点执行）；保存时校验只收只读 | `loop.py`、`core/questions.py`、前端 | 单测：含手机号的问题可命中；相似反例不误命中（阈值用例）；DML 保存被拒 |

### WS6 · B3 代号化闭环（D9；独立，可先行）

| 任务 | 涉及 | 验收 |
|---|---|---|
| 执行前还原：tool args 先 `decodify_text` 再过闸门执行 | `ai/tools/sql.py` | 端到端：敏感表 AI 查询不再报 `no such table: t_17`（审计 B3#3 场景） |
| 往返不变式：tool result 回喂前 codify（列名/表名），模型可见世界恒代号 | `ai/tools/sql.py` | 单测：回喂内容无真实敏感表列名 |
| 审计记录真名 + 与清单代号的对应说明 | `audit/logger.py` 调用处 | 审计条目断言 |
| codify 模块单测补齐（现零覆盖，审计结论） | `tests/` | 覆盖生成/还原/冲突递增 |

### WS7 · 平台工具批次 1 + 技能开关（D13/D14/D15；依赖 WS1 的 enabled 感知）

| 任务 | 涉及 | 验收 |
|---|---|---|
| 工具注册规格扩展：trust(只读/变更/破坏性)/confirm/audit 元数据 | `tools/registry.py` | 单测：无 trust 元数据的工具拒绝注册 |
| `query_audit` + `get_last_operations` 工具（结果回喂过脱敏管道）+ `audit_qa` 技能 | 新 tools + `skills/builtin/` | e2e："我刚才干了什么"得到正确审计回顾；结果经 redact |
| 系统操作审计 `source=system_tool` | `audit/logger.py` 调用处 | 每次平台工具调用有审计条目 |
| 技能开关完善：地板（query/refusal 常开）、降级文案（WS1 已做）、勾选变更写审计 | `api/settings.py`、前端设置页 | 单测：地板不可关；勾选产生审计事件 |
| 团队模式：组织策略 ∩ 个人勾选（最严者胜） | settings/权限层 | 单测：成员勾选不能恢复管理员禁用项 |

### WS8 · 前端 4 步与确认流（草稿问题 3；随各 WS 的前端任务落）

| 任务 | 涉及 | 验收 |
|---|---|---|
| 真 4 步：去 `playSteps` 假定时器、ThinkPanel 默认展开、`stage/think/sql_card/gate` 真驱动、`detail` 对齐 `context_meta` | `AiRail.tsx` | e2e：意图/表检索步骤显示真实 tags/candidate_tables |
| DML 确认卡三态（执行/取消/过期）与问题库确认卡 | `AiRail.tsx` | e2e：确认/取消/过期三条路径 |
| SSE 事件契约表落地（类型 × 载荷 × 顺序不变式，manifest 先于任何 text） | 08 §7 + 前后端 | 契约测试 |

---

## 七、实施顺序与新人导读

**依赖关系与建议顺序**：

```
WS0（文档）
  └─> WS1（preflight，地基）
        ├─> WS2（新技能，轻，快赢）
        └─> WS7（平台工具+开关）
  WS5（检索/问题库）与 WS6（B3 闭环）独立，随时可并行 -- 建议尽早做 WS6，
  它是修"核心路径断裂"的 bug 而非新功能
  WS3（会话改造，面最大）──> WS4（DML 协议，pending 状态依赖 WS3 的 session 存储）
  WS8 随各 WS 的前端任务分散落地，不单独立项
```

**新人怎么干活**（与手册 README A 的标准任务循环对齐）：

1. 从第六节领一个任务行，去 02 找到 WS0 补的对应条目（这是唯一需要完整精读的需求）；
2. 涉及交互/视觉先查 03/04 对应节；动检索先读 07 §6/§7；动意图/编排读本文 D 决策节；
3. 实现前用两三句话向负责人复述理解，确认后动手；
4. 验收 = 逐条对照本表验收列 + 02 条目验收标准，附测试输出；**测不过就是没做完**；
5. 行为变了就同步改文档（08/02/03），文档代码一起交付。

**三条全程有效的铁律**（违反任何一条都算做错）：① 行数据不出网、工件进出模型上下文恒经脱敏管道（D9）；② 模型可见世界恒代号（D9）；③ 技能只收窄不扩权、禁止靠缺席（D12/D13）。
