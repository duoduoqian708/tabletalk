# NL2SQL 知识库与引擎设计方案

> 日期：2026-08-29 · 状态：设计稿 · 定位：自包含的目标架构蓝图
> 修订：2026-08-31 -- §9.2 表级过滤器执行点调整：引擎不参与 SQL 内容组成（校验拦截/安全审计/执行之外不改写 SQL），过滤器改为 context 提示消费，见 §9.2 与 D13。
> 修订：2026-08-31 -- §6/§7/D11/D12 建边管线简化：构建来源收敛为三类（FK/命名推断/LLM 识别），值重叠与判别器检测删除；**一切边（含置信度 1.0 的 FK）经人工确认才生效**；人工连线与查询日志挖掘是生命周期机制（非构建来源），其中日志挖掘产出的边同样过确认闸门。
> 修订：2026-09 -- 意图识别移除关键词层（§13/D6）：意图统一由 LLM 产出 TaskPlan；mock/离线确定性降级；未来向量层做语义沉淀（相同语义问题直接复用执行路径，跳过 LLM）。

---

## 0. 文档定位

本文档定义系统重构后的目标架构，覆盖两块：

1. **知识库**：把表 ER 图当作天然的知识图谱来构建——因为多表查询的难点本质是 join 路径
2. **引擎**：Plan-and-Execute + ReAct 的分层循环——意图分解 → 任务循环 → skill 循环 → 工具

两套核心思想：**图管结构、知识库管语义**；**意图是剖面不是标签，skill 是配置不是代码**。

---

# 第一部分 · 知识库设计

## 1. 核心思想

**单表查询简单，多表查询难。多表查询的本质是找 join 路径。ER 图天然编码了 join 路径，因此图结构本身就是知识图谱。**

- embedding 检索能召回"相似 DDL/文档"，但**天然不含 join 拓扑**
- 只有图能告诉模型"哪些表能连、通过哪列连、连几跳"——这是图不可替代的价值
- 业界与学术有据：知识图谱用于 Text-to-SQL 多跳查询、KG+RAG 混合框架、property graph schema

**图在系统里承担双重角色**：

1. **检索器**：把候选 join 路径喂给 LLM，缩小搜索空间
2. **校验器**：LLM 生成的 join 若不在图里，拦截并要求重写（plausibility gate）——这比检索更强，因为它把"幻觉 join"变成可验证的错误

**图是"检索器 + 校验器"，不是"推理器"**——它只回答结构问题（哪些表能连、怎么连），不回答语义问题（口径是什么、怎么算）。

**确定性的双重含义**：图构建算法是确定性的（同一 schema 永远产出同一张图，可复现、可测试、无 LLM），但推断出来的边天然不确定，所以边必须携带置信度——"算法确定性"≠"结果确定无疑"。

## 2. 三层模型

把知识图谱拆成三个叠加层，各司其职：

| 层 | 名称 | 内容 | 职责 |
|---|---|---|---|
| L1 | 结构层（ER 骨架） | 表、列、join 边 | 回答"连得对"——join 拓扑 |
| L2 | 语义层（业务血肉） | 描述、口径、同义词、标签、枚举 | 回答"算得对"——业务语义 |
| L3 | 行为层（肌肉记忆） | 查询日志、边加权、few-shot | 回答"越用越准"——从真实使用中学习 |

- **L1** 决定哪些表能连、通过哪两列连、连几跳
- **L2** 决定"收入的口径""活跃用户怎么定义"这类语义，挂在结构节点上
- **L3** 让知识库越用越聪明——这是"知识库"和"schema 导出"的本质区别

## 3. 图数据模型

### 3.1 节点与边

```
节点：表（table）
边：join 谓词（列对列表），带属性
```

**边的身份 = 列对，不是表对。** 一条边由 `(source_table, target_table, cols)` 唯一标识（cols 为列对列表，见 §3.2）。

**多重图**：同一对表之间允许**多条平行边**。例：

```
orders.user_id     = users.id    (N:1)   ← "下单的客户"
orders.shipped_by  = users.id    (N:1)   ← "发货的运营"
orders.approved_by = users.id    (N:1)   ← "审批人"
```

三条边互不冲突，每条由列对区分。**绝不允许按表对去重**——那样会吞掉多列对信息，退化成"模型猜哪列"。

### 3.2 边的属性

```
{
  source_table, target_table,
  cols: [ (source_col, target_col), ... ],   # 列对列表：复合键 join 需要多对列（AND 连接）
  cardinality: "1:1" | "1:N" | "N:M",
  relation: "fk" | "naming" | "value_overlap" | "query_log" | "user" | "same_dimension",
  confidence: 0~1,             # 置信度（推断边低，确认边高）
  provenance: "declared_fk" | "naming_inference" | "value_overlap" | "query_log" | "human",
  guard: "X.type = 1" | None,  # 守卫谓词：多态关联的成立条件（可选，见 §3.4）
  metadata: {...}              # 可挂语义标签、引用语义层条目 id
}
```

要点：

- **cols 是列对列表**（不是单列对）：复合键/多列 FK 的 join 条件 = 多对列 AND 连接（`A.order_id=B.id AND A.line_no=B.line_no`）
- **允许自环**：`source_table == target_table`（如 `employee.manager_id → employee.id`），BFS 用 visited 防死循环
- **guard**：守卫谓词，仅在多态关联（type+code）时存在，见 §3.4

### 3.3 列节点：懒晋升

**列成为节点 ⟺ 它有边（join 边或语义边）或挂了标签。** 其余列不占图空间，活在 schema 摘要和向量库里。

- 不做全列节点：节点数会 O(表数×列数)，80% 是孤立节点，纯噪音
- "归属边"（列属于表）**不物化成图边**，它是隐式层级，留在 schema 属性里
- 列升级成节点的时机：挂了语义标签、值落地映射、一列多边、跨表同义
- 字段间的连线 = join 谓词本身；列是从边里"长"出来的端点，不是预先铺满的

### 3.4 守卫边（条件边）

**场景：多态关联（polymorphic association）**——`X` 表的 `type + code` 两列，`type=1` 时 `code` 关联表1、`type=2` 时关联表2（Rails 的 `commentable_type+commentable_id`、附件的 `owner_type+owner_id` 都是它）。

这不是一条简单边，而是**带条件的边**：

```
edge: X.code → table1.code, guard: "X.type = 1"
edge: X.code → table2.code, guard: "X.type = 2"
```

序列化成路径时，守卫边产出**完整可执行条件**：

```
X.code = table1.code AND X.type = 1
X.code = table2.code AND X.type = 2
```

**检测（2026-08-31 修订）**：判别器感知的值重叠检测已随建边管线简化删除（见 §6）；守卫边由 **LLM 识别提案**（draft）或人工录入产生，LLM + 执行反馈兜底。

> 历史方案（保留参考）：表里有低基数字段（`type`/`_type`/`kind`）+ `code`/`_id` 字段时，按判别器分组采样，组内值对候选表同名列算包含率，分别高包含则推断守卫边。采样分组格式（`_grouped`）在 `sample_values` 中仍保留。

## 4. 双写架构：图管结构、知识库管语义

**图负责结构，知识库负责语义**，两个库**共用同一个身份标识 `(表名, 列名)`** 引用同一对象。

```
图（结构层）：表 = 节点，边 = join 谓词          → "哪些表能连、怎么连"
知识库（语义层）：字段描述 / 示例 / 枚举 / 同义词  → "这个字段是什么意思"
```

### 4.1 查询时的检索顺序（关键，顺序反了精度崩）

```
问题 → ① 意图/图确定种子表 → ② 图扩展出候选 join 路径（缩到几张表）
     → ③ 知识库检索【限定在这几张表的列范围内】→ ④ 组装 prompt
```

知识库检索**必须限定在图选定的表集合内**，否则"销售额"会从无关表里捞出 `amount` 字段。

### 4.2 身份一致性与同步

- 字段改名 → 语义层条目变孤儿、图边失效；必须**检测并提示维护**
- 新表/新列 → 图可自动重建，语义层需提示补条目
- 统一身份 + 变更传播是双写架构的**首要运维成本**，必须有机制承载

### 4.3 跨表同义列

**不同表里各自有一个列，指代同一个业务维度**（如 `orders.region`、`users.region`、`stores.region` 都是"地区"）。

- 这不是 join 边（不构成 join 条件），也不是单列描述，**必须明确归属**：语义层做交叉引用条目，或图加一种不参与 join 的 `same_dimension` 边类型
- 用途：值落地共享、过滤选对列、避免误当 join
- **警惕**：列名相同 ≠ 语义相同（`status` 可能是订单状态也可能是库存状态）——同义列归属**必须人工确认**，不能靠名字自动推断

## 5. 概念字典（维度字典）

把共享的业务维度（如"地区"）抽成**概念条目**，单独维护一份规范枚举，成员列各自带值映射。

```
concept: 地区
canonical_enum: [{code: EAST, label: 华东}, {code: WEST, label: 华南}]
members:
  - {table: orders,  column: region, mapping: identity}                 # 值即规范枚举
  - {table: users,   column: region, mapping: {华东: EAST, 华南: WEST}}  # 中文名 → 枚举
  - {table: stores,  column: region, mapping: {1: EAST, 2: WEST}}       # ID → 枚举
updated_at / 来源（人工确认 or 采样）
```

三条铁律：

1. **共享的是"概念 + 规范枚举"，列各自带值映射**（值可能不一致，不能字面共享）
2. **成员列归属必须人工确认**（同名不同义会误伤）
3. **定期采样刷新枚举**（SELECT DISTINCT 抽少量值比对），防语义层和数据库漂移；发现新值提示人工确认，不自动写

**概念字典的扩展——静态业务常量**：除枚举外，概念字典还承载不随时间变化的业务常量（税率、阈值、魔数），供 LLM 生成 SQL 时引用。区别于运行时变量（§8）。

## 6. 建图管线：三类构建来源（2026-08-31 修订）

**裸 FK 建边必死**（MySQL/PG 的 FK 是可选约束，大量库没有）。**铁律：不管什么方式形成的边，最终都要人工确认才生效--置信度 1.0 的 FK 也要标记出来等人点头。**

构建来源（重建时生产）：

| 来源 | 方法 | 置信度 |
|---|---|---|
| 声明 FK | information_schema / PRAGMA | 1.0 |
| 命名推断 | `user_id` 列 → `users.id`（类型族校验、通用名/自环排除） | 0.6 |
| LLM 识别 | 全局扫描 + 候选裁决，产出 draft 边（含守卫边建议） | draft |

非构建来源（生命周期机制，不属于重建管线）：

| 机制 | 说明 |
|---|---|
| 人工连线 | 图编辑器手动加边（user），直生效，不参与重建 |
| 查询日志挖掘（L3） | 审计日志实际 join 过的表对 → query_log 边**投递 draft 队列**，同样过人工确认闸门 |

设计点：

- **删除了值重叠探测与判别器感知检测**（2026-08-31）：采样依赖重、阈值参数多、误报空间大，是管线最脆弱的部分；非 FK 关系（含多态守卫边）交由 LLM 识别提案
- **确认闸门统一**：确定性来源（fk/naming）与 LLM 提案、日志挖掘同走 draft → 确认；不确认不生效（图校验/路径串/扩展只消费已确认边；图未就绪时校验跳过）
- **重建语义**：重建只重新生产 draft；已确认边与人工连线保留（生命周期资产），按 schema 变更过滤失效边；draft 与已确认边重合自动去重
- **增量更新**：schema 变更、新表出现时增量重建，非一次性全量
## 7. 构建流程：确定性结构层与 AI 语义层分离

构建过程的核心是**把"确定性结构"和"AI 语义"彻底分开**，而不是混在一个时间轴上：

### Phase A · 确定性建图（无 LLM，秒级，模型无关）

```
1. schema 发现：表 / 列 / 类型 / 主键 / 已声明 FK / 行数
2. 确定性建边：FK + 命名推断（2026-08-31 起值重叠/判别器检测已删）
3. 产出：draft 边列表 + 置信度 + provenance（2026-08-31 起不再直接进正式图）
```

**这一层绝不碰 LLM**——图在第一步就建好，语义层挂不挂、AI 调不调，都不影响"哪些表能连"。

### Phase B · AI 语义增强（LLM，产出全是 draft）

```
B1 逐表/逐列注释：comment（业务含义）+ values（枚举）+ example
B2 概念字典提取：同义列分组 + 规范枚举 + 值映射
B3 领域标签：表归属的业务域
```

- **B1/B2/B3 无数据依赖，可并行**
- **AI 只做语义，不做结构**（图已在 Phase A 定好）
- 关键约束：AI 输入是「Phase A 的图 + 采样值」，AI 可以**建议修正边**（如"这条命名边是错的"），但**不能凭空造边**——造边是 Phase A 的确定性职责；LLM 识别是**提案**（draft），与确定性边同样过人工确认

### Phase C · 人工确认（draft → confirmed；2026-08-31 起覆盖一切边）

```
1. 审查草案：注释 / 概念 / 标签 / 全部候选边（含 FK/命名推断/LLM 提案/日志挖掘）
2. 确认 → 升为权威（FK 置信度 1.0 同样标记出来等人确认）
3. 拒绝 → 回退；重建只重新生产 draft，已确认边与人工连线保留
4. 确认后 → 向量化（延迟到这里，避免白做）
```

**2026-09 修订——全量重建保留当前生效，提案并行**：

- 重建不再清空当前确认成果：注释/标签/表绑定/已确认边/向量全部保留，只做 schema 对齐（删表删列）+ 对新内容重新提案
- 字段/表级：AI 产出写 `proposed_*`（本轮提案），与当前生效值并存；确认=提案提升为当前，保持当前=清提案。审查页提供逐字段「对比」按钮（当前 vs 提案）与「全表取新/全表保持当前」
- 标签：本轮提案进「待审批」列表，当前生效标签在「当前标签」列表（两列并行，重名不去重，用户裁决保留/删/复用）
- 图边：重建记录提案基线，正式图中未被重新提案、端点仍存的边标红（删除建议）、draft 中新边标绿、同列对基数变化标黄；红边「保留」打 pinned 标记跨重建存活；确认全部/放弃后 diff 结束恢复灰态
- stash 双缓冲移除（当前生效从不在位让路，放弃=清提案，无需回滚）

### Phase D · 行为层回灌（持续，非一次性；2026-08-31 起挖掘边同样过确认闸门）

```
1. 查询日志挖掘：实际 join 过的表对 → query_log 边投递 draft 队列（确认后生效）
2. 边加权：成功查询 → 经过的已确认边 weight+
3. few-shot：成功问答对入库，作为 in-context 样例
```

## 8. 运行时会话变量与静态业务常量

平台不接鉴权系统，但查询场景需要"当前租户/当前用户/当前时间"等参数。**这些是运行时变量，不是常量**，不能写进知识库（知识库写死 `user_id=123` 会错人、会泄露）。

### 8.1 会话变量池（运行时）

LLM 需要的不是"值"，而是"**能引用的名字**"——写 SQL 时引用占位符，执行层再替换成真实值：

```
LLM 写：  SELECT ... WHERE tenant_id = :current_tenant AND user_id = :current_user
执行层：  :current_tenant → 实际租户值（从连接配置/会话取）
```

设计要点：

1. **命名占位符**：系统定义一批会话变量（`:current_tenant`、`:current_user`、`:current_date`、`:current_org`...），带名称+含义+类型，注入 context 让 LLM 知道有哪些可用
2. **值在执行时替换**：由查询层/安全闸门统一替换，LLM 永不接触真实值
3. **默认值来源**（不接鉴权）：
   - 连接级配置：每个数据源连接配一个"默认租户/上下文"
   - 全局设置：单租户部署下一个全局默认值
   - 系统时钟：`:current_date` 等时间变量

### 8.2 静态业务常量（知识库）

不随时间变化的业务常量（税率、阈值、魔数）放**概念字典扩展**（§5），供 LLM 引用。

### 8.3 与表级过滤器的关系

表级过滤器（§9）用的就是会话变量：`tenant_id = :current_tenant`。两者配合：

- **表级过滤器**：把"这张表应该带 `tenant_id = :current_tenant`"的规则挂表上（context 提示，LLM 自觉携带，引擎不强制注入--见 §9.2 修订）
- **会话变量池**：定义 `:current_tenant` 占位符的值从哪来、怎么替换

### 8.4 安全意义

会话变量间接引用是**安全机制**：

- LLM 硬编码 `user_id = 123` → 可能用错用户、泄露 ID
- LLM 写 `user_id = :current_user` → 系统替换成**正确**的值，LLM 看不到真实值

这和参数化查询防 SQL 注入是同一道理——**间接引用让"错误值注入"和"敏感值泄露"都变难**。

## 9. 表级过滤器（软删除 / 租户 / 数据权限）

**场景**：几乎每张表都有 `is_deleted`、`tenant_id`、`org_id`，每个查询和每次 join 都要带。漏掉 = 查错数据（查出已删除的、别的租户的）。

### 9.1 与守卫边的区别

| | 表级过滤器 | 守卫边 |
|---|---|---|
| 挂在 | 表上 | 边上 |
| 谓词依赖对方表吗 | ❌ 不依赖 | ✅ 依赖 |
| 何时生效 | 该表**一出现**就要带（含单表查询） | 仅当**遍历这条边**时带 |
| 回答的问题 | 哪些行"算数/可见" | 这条边在什么条件下"成立" |

判别标准一句话：**表级过滤器的谓词不依赖对方表，守卫边的谓词依赖对方表（用于区分连哪张表）。**

### 9.2 定义位置与消费位置（2026-08-31 修订）

- **定义**：知识库（L2 语义层），作为表的语义属性，和表注释/概念同类，需人工确认
- **消费**：作为 context 知识注入 prompt--图扩展顺带收集每张表的 filter 规则，随候选 join 路径一起给 LLM，由 LLM 生成 SQL 时自觉携带
- **引擎红线（修订后的硬边界）**：**引擎不参与 SQL 的内容组成**--查询层/安全闸门不做强制谓词注入、不改写 SQL 文本。引擎只管三件事：
  1. **校验拦截**：plausibility gate（图外 join 打回）+ 安全闸门（REVIEW/BLOCK 判定）
  2. **安全审计**：每次执行落审计日志
  3. **执行**：跑 SQL（含会话变量占位符 `:current_tenant` 的 AST 级值替换--值替换属执行职责，不是内容改写）
- **取舍说明**：放弃强制注入换来的不变式是「SQL 文本 = LLM 意图的原样呈现」，所见即所执行，审计与回放不被引擎痕迹污染；代价是漏带 `is_deleted=0` 这类风险回到 LLM 一侧，靠 context 提示 + 执行反馈循环（空结果提示）兜底
- **图不管 filter**：图（L1）只管关系，filter 是单表属性，不是关系

### 9.3 粒度

```
连接级默认：multi_tenant=true, tenant_col=tenant_id     ← 全库多租户
表级覆盖：  orders: {soft_delete: is_deleted=0}          ← 软删除表级
           system_config: {tenant: exempt}               ← 系统表豁免租户过滤
```

- **软删除**：表级（每张表是否软删除、用哪个列，各不相同）
- **租户过滤**：连接级默认 + 表级覆盖（系统表豁免）

### 9.4 结构检测 → 语义确认

与建边同模式：

```
结构层（检测）：发现表里有 is_deleted / deleted_at / tenant_id 这类列 → 产出候选
语义层（确认）：确认"这张表的软删除列是 is_deleted，恒过滤 =0" → 权威 filter 定义
```

## 10. 查询时消费流程（图的三个触点）

```
NL 问题
  → ① Schema Linking（意图层，与图无关）确定种子表
  → ② 图扩展（确定性、可测试、模型无关）BFS 2~3 跳，输出候选路径串
  → ③ Prompt 组装（schema 摘要 + 候选 join 路径 + 相似问答 + 相关文档 + 会话变量清单 + 表级过滤器）
  → ④ LLM 生成 SQL（可多步工具调用）
  → ⑤ 图校验（plausibility gate）join 不在图里 → 打回重写
  → ⑥ 执行反馈循环（报错/空结果 → 反馈 → 重生成）
  → ⑦ 入库回灌（成功查询 → 边加权 + few-shot 入库，即 L3 层）
```

要点：

- **② 必须服务端确定性完成，不进 LLM 循环**——检索是纯逻辑，可单元测试、模型无关（与安全闸门哲学一致）
- **③ 编码方式**：不 dump 邻居列表，dump **路径串**（`A.col = B.col (N:1)`，守卫边带 `AND guard`），LLM 直接可用
- **⑦ 是 L3 行为层**：每次成功执行的查询记录实际 join 路径，边加权 + 进 few-shot 库

## 11. 非常规场景与边界（真实 schema 的硬情况）

按「冲突严重度 × 常见度」梳理，标注覆盖状态：

| 场景 | 例子 | 覆盖状态 |
|---|---|---|
| 复合键/多列 FK | `order_item(order_id, line_no)`、`(tenant_id, entity_id)` | ✅ 边模型 cols 列对列表 |
| 自引用 | `employee.manager_id → employee.id`、BOM | ✅ 允许自环边 |
| 多态关联 | `type+code`，type 区分连哪张表 | ✅ 守卫边（§3.4） |
| 软删除/租户/权限 | `is_deleted`、`tenant_id` | ✅ 表级过滤器（§9） |
| 未声明 FK | `order.user_id` 无 FK 约束 | ✅ 命名推断 + 值重叠（§6） |
| 采样偏差 | `ORDER BY pk DESC` 漏历史枚举值 | ✅ 采样分列策略（§6） |
| 一列多义 | `status[type=1]` 与 `status[type=2]` 含义不同 | ⚠️ 概念字典带条件归属，人工确认 |
| 视图 | join 被物化，看不到底层拓扑 | ⚠️ 视图作普通节点，不展开内部 join |
| 非等值/范围 join | SCD 时态表（`date BETWEEN eff_start AND eff_end`） | ⏳ v1 不建模，语义层记录 + LLM/执行反馈兜底 |
| 跨 schema / 命名不规范 | `hr.employee` vs `sales.employee`、`Users` vs `users` | ⚠️ 表身份 schema.table，命名 normalize |

**待后置**：非等值/范围 join（SCD）最复杂、易误报，v1 不自动建模，作为语义层知识交给 LLM + 执行反馈处理。

---

# 第二部分 · 引擎设计

## 12. 总体架构：Plan-and-Execute + ReAct

整个引擎 = **Plan-and-Execute（先规划再执行）+ ReAct（思考-行动-观察循环）**。

```
用户请求
  → 意图层（Plan）：分解成任务列表 TaskPlan
  → 任务循环（Execute 外层）：顺序执行每个任务
       → 路由决策：任务剖面 → 一个 skill
       → ReAct 循环（内层）：skill 内反复 思考→调工具→观察，直到终止条件
  → 结果组装（编号列表）
```

三层性质（**不是三层循环**）：

| 层级 | 本质 | 是否循环 |
|---|---|---|
| 任务 | 分解出的工作单元 | ✅ 任务循环（顺序执行器） |
| skill | 该任务的执行方案 | ✅ ReAct 循环 |
| tool | 原子执行单元 | ❌ 一次调用一次返回 |

- **路由（任务 → skill）是决策，不是循环**
- **安全闸门横切所有层**，不属于任何一层

## 13. 意图层：任务分解 + 三轴剖面

### 13.1 输出：TaskPlan（任务列表，长度 ≥ 1）

```
TaskPlan = [
  { action, trust, modality, target },
  { action, trust, modality, target },
  ...
]
```

- 单意图请求 → 长度 1
- 复合请求 → 长度 > 1（不需要单独的 compound 布尔）

### 13.2 三轴剖面

```
action    : query | write | ddl | kb | schedule | system | unknown   ← 封闭枚举
modality  : answer | analyze | report | automate                     ← 有序枚举（可升降级）
trust     : read | write | ddl                                       ← 派生值（不分类）
target    : {concept?, tables?}                                       ← 自由抽取（非枚举）
```

**关键设计**：

1. **trust 不分类，由 action 查表派生**（query→read, write→write, ddl→ddl），避免识别出矛盾状态
2. **action 是封闭枚举，必须带 `unknown` 兜底值**
3. **modality 是有序枚举**：`answer ⊂ analyze ⊂ report ⊂ automate`，呈包含关系，可沿尺度升降级
4. **target 是自由抽取**，与路由无关，只影响 prompt 组装

### 13.3 分解要语义判断，不能按标点拆

- "查一下 xxx，顺便看下 xxx" → 拆 2 个任务（不同操作）
- "查每个季度的销售额**和**利润" → **1 个任务**（同一 SQL 取两列）

依据是"**是否独立操作**"：不同表/不同动作 → 拆；同一查询取多指标 → 不拆。

### 13.4 意图澄清闸门（2026-09 草案）

**场景**：意图**不完整/不清晰**——缺目标（"帮我查一下"无对象）、范围不明（"最近"到
多久）、空指代、或信息冲突。此时不应盲目执行或硬塞进 query。

```
用户：帮我查一下
  → decompose（LLM）判定意图不完整 → 产出 clarify 信号 + 候选问题（1~3 个）
  → 引擎暂停执行，发澄清卡片（不进任务循环/工具/检索）
  → 用户回答 → 并入上下文重新 decompose（或带澄清继续原计划）→ 正常执行
```

**关键点**：
- 这是 decompose 的新输出态（区别于 `unknown`=offtopic；新增 `clarify` 通道），
  LLM 判定"意图不完整"本身要克制——只对缺关键目标/范围时触发，避免每问必澄清
- 澄清交互接入 §19.5 continuation gate（用户回答作为 new_question + 澄清上下文）
- 执行前刹停（不发 task_start），与 report 澄清（流程中途）层级不同

## 14. 任务循环的执行规则

```
for task in TaskPlan:
    route(task) → skill          # 一个任务对应【一个】skill
    skill 内部 ReAct：用【多个】tool 反复 思考→调用→观察
    结果写入共享 Context（前序任务结果对后续可见）
```

三条硬规则：

1. **一任务一 skill**（skill 不调 skill）——避免在任务和 skill 之间再引入编排层
2. **顺序执行**：按用户顺序，不过度设计并行；跨任务依赖靠共享 Context 传递
3. **失败即停**：前序任务失败 → 后续任务停，**尤其任何写任务之前必须确认依赖的读都成功**

## 15. Skill 设计：声明式配置（数据，不是代码）

### 15.1 第一原则

**skill = 配置数据，不是代码类。** 真正的执行器是**一个通用 ReAct 循环**，读配置跑。

- 加新 skill = 加一条配置，不改执行代码
- 配置可 schema 校验、可 diff、可 review

### 15.2 Skill 完整结构

```yaml
name: report
description: "生成带叙述的报表（聚合数据 + 数字可追溯）"

# ① 路由匹配条件
match:
  action: query
  modality: report

# ② 能力边界
trust: read                                   # 声明信任姿态
tools: [get_schema, kb_read, graph_read, run_query]   # 白名单

# ③ 场景指导（说明书正文）
system_prompt: |
  你是报表生成场景。流程：
  1) 查知识库确认口径
  2) 写聚合 SQL
  3) 执行并核对数字
  4) 写成带叙述的报表，数字可追溯
  ...

# ④ 循环控制（一等字段，别忘）
termination:
  done_when: "报表已产出且数字已核对"
  max_turns: 6

degradation: "知识库无命中 → 退回纯 schema 生成；行数超限 → 提示收窄条件"
```

### 15.3 Skill 集合（7 个，按工具边界/信任姿态划分）

| # | Skill | 信任 | 工具白名单 | 说明 |
|---|---|---|---|---|
| 1 | query | read | get_schema, kb_read, graph_read, run_query, query_audit | 即席查询/分析，输出表格 |
| 2 | report | read | get_schema, kb_read, graph_read, run_query | 聚合 + 叙述，强制 include_data |
| 3 | write | write | run_query, get_schema, run_dml | DML 草稿 + 确认，永不自动执行 |
| 4 | ddl | ddl | get_schema, draft_ddl | DDL 草稿，**无执行工具** |
| 5 | kb | write | kb_read, kb_write, graph_read, graph_write, get_schema | 知识库/图谱维护 |
| 6 | schedule | write | manage_task, run_query, get_schema | 定时任务 CRUD |
| 7 | general | read | get_schema, kb_read, graph_read, run_query | 兜底，只读低危，永不拒绝也不闯祸 |

- **answer 和 analyze 共用 query skill**（差异只在循环轮数/终止条件），不为每个 modality 建 skill
- **general 兜底必须有**：承接未知组合、置信度低、组合意图拆分失败的请求

### 15.4 路由：查表 + 兜底

```
route(action, modality) → skill_name      # 纯函数，可穷举单测

(query, answer/analyze) → query
(query, report)         → report
(query, automate)       → schedule
(write, *)              → write
(ddl, *)                → ddl
(kb, *)                 → kb
(schedule, *)           → schedule
(unknown, *)            → general
```

## 16. 安全与隐私：横切 + 防御纵深

安全不属于任何一层，是**每一层的公共约束**。

### 16.1 两道独立的墙

```
墙1（能力层）：skill 的 trust 声明 + 工具白名单
              → 查询任务根本看不到写工具（减少攻击面 + 减少 LLM 选错）
墙2（执行层）：全局安全闸门
              → 即使写被尝试，也要 preview + confirm（最后防线）
```

- **skill 只声明信任，不负责执行检查**；闸门全局执行
- 两道独立机制：skill 声明错了 trust，闸门仍拦得住；闸门有 bug，白名单还在兜底
- **写任务永远过确认闸门**，复合请求绝不绕过
- **注意**：SQL 闸门管不了非 SQL 工具（kb_write / graph_write / manage_task），这些工具的能力隔离**只能靠 skill 白名单**——这是 skill 白名单不可删的原因之一

### 16.2 硬性红线（产品护城河）

1. **DDL 永不自动执行**：AI 只有 `draft_ddl`（生成草稿），执行永远靠人工
2. **写操作永不自动执行**：DML 一律 preview + confirm
3. **行数据最小出网**：默认只给模型结构（columns + rowcount），行数据是 request 级 opt-in（限 N 行）
4. **每次模型调用可审计**：token/成本/调用链全程记录

## 17. 审计：横切 + 调用链

- 每次 tool 调用记录：**哪个 skill 触发的、参数、结果、verdict**
- skill → tool 调用链是调试数据和 L3 行为层的训练素材
- 审计不属于任何一层，是公共约束

## 18. Context：层间 I/O 契约

各层传递统一的上下文对象，只认这个对象，不互相直接调内部：

```
Context = {
  conn_id,           # 连哪个库
  session_id,        # 会话
  tasks,             # TaskPlan
  current_skill,     # 当前 skill
  selected_tables,   # 图/知识库选出的表集合（跨层共享）
  sql_draft,         # SQL 草稿（skill 与 tool 间传递）
  task_results,      # 前序任务结果（供后续任务/依赖读取）
  include_data,      # 是否允许行数据
  session_vars,      # 会话变量（:current_tenant 等，执行层替换）
}
```

**结果块统一结构**（外层任务循环据此组装）：

```
{ type: "table" | "report" | "confirm" | "text", content: ... }
```

## 19. 结果组装

按任务顺序组装编号结果：

```
1. 经过查询：结果 xxx（type: table）
2. 经过查询：结果 xxx（type: table）
3. 已生成 DML 草稿，可手动执行 [执行]（type: confirm）
```

- 简单多查 → 确定性拼接
- 报表类 → 最后 LLM 汇总成文
- 复杂度集中在第 2 步 ReAct 循环；第 1 步（拆解）和第 3 步（整理）尽量轻、尽量确定

## 19.5 追问/续流统一入口（continuation gate，2026-09 草案）

**问题**：用户对 AI 一个回合的"反应"目前是三种拼凑的链路——追问建议走全新问题流程、
报告澄清用 clarify 系统消息硬塞续流、SQL 补充选项走独立 /ai/sql-option；外加遗漏的
DML 确认（"预览影响行数，点确认执行"）也是同一类"用户对回合的反应"。四个入口协议不一，
上下文拼法各异，属临时拼凑。

**方案：不做"一个 skill"，做"一个续流入口 + 四种 type"**（引擎执行层不动，收敛的是入口与上下文契约）。

```
用户回应对 AI 回合（点击卡片/按钮/输入）
   → POST /ai/continuation  { type, payload, session_id, ... }
        new_question  → 完整意图流程（decompose → plan → execute_plan）【上下文延续】
        clarify_report→ resume 当前报告流（报告任务未结束，补澄清后继续）
        sql_option    → 轻量改写 SQL 并重跑（不进意图分解/检索）
        confirm_write → DML 确认执行（preview 已给过，token 闭环后执行）
   → 统一带会话上下文，统一审计/清单
```

**两条硬约束**：

1. **new_question 必须走完整意图分解，不能退化成"追问 skill"**——追问点出的可能是
   schedule/write 等任意意图（如"把这统计改成每周自动跑"），塞进固定追问 skill 会丢意图分解。
   所以 type 的分流在**入口层**（HTTP 路由/协议），不是引擎的 skill 层。
2. **new 与 continuation 必须可区分**：new_question 是新上下文（喂给 decompose/计划），
   clarify_report / sql_option / confirm_write 是续旧上下文（喂给进行中的任务/查询）——
   误标会让新问题错误地续到旧任务上。协议层用 type 字段守这个边界。

**当前四条的既有落点**（设计挂实现，改造是对齐而非推倒）：
- ① new_question → `POST /ai/chat`（已存在，语义即是全新流程）
- ② clarify_report → `POST /ai/chat` + clarify 系统消息重传续流（ai.py `clarify` 事件）
- ③ sql_option → `POST /ai/sql-option`（独立轻量调用，suggest/options 回显）
- ④ confirm_write → DML 确认执行（confirm_token + pending 闭环）

**落地顺序**：设计定稿 → 后端新增 continuation 入口（type 路由，保守保留既有端点反代）→
**前端仍随整体大改**（一个可点反应片组件，四种 type 的统一渲染）。
## 19.6 实时任务流前端协议（2026-09 草案）

**目标**：前端展示"实时任务流"——每个任务当前执行到哪一步/哪个工具，随后端实际调用
动态生成并实时更新；**不是一份固定 tool 展示条模板**（现状 AI Rail 的 STEP_DEFS 静态步槽
是"场景对应一组固定槽位"，真实事件只填槽，与后端实际调用脱节）。

**协议：纯事件驱动，复用现有 SSE 事件**（无需新事件源）：
```
task_start{id,skill,action}          → 任务节点出现（TaskPlan 每任务一条，逐条来）
├ subtask_start{id,tool,label}        → 工具条出现（仅工具真的被调用才有）
│   ├ subtask_progress{id,tool,delta} → 日志流逐行（调用 + 入参摘要 + outcome.think）
│   ├ think{text}                     → 思考行
│   └ subtask_done{id,tool,status}    → 工具收尾（ok/error/blocked，error 带 detail）
task_result{index,result_type,...}    → 任务结果
task_done{id,ok,...}                  → 任务收尾
```
- **动态性由事件本身保证**：工具条 = subtask_start 的投影，不存在的工具不出现；
  ReAct 多轮循环 → 工具条逐行追加；不绑定任何场景模板。
- **任务级**：task_start/result/done 组成任务列表（复合请求可并列若干任务的实时进度）。
- **后端补充**（已实现）：subtask_progress 携带工具入参摘要，日志流完整
  （进入 → 入参 → 思考 → 结果/错误）。

**前端大改造切片**（与 §19.5 续流、AiRail 收拢同轮）：
- 拿掉 STEP_DEFS 静态步槽，改为"任务流树"状态模型（tasks[] → 每任务 tools[] → 每工具 logs[]）
- 纯事件增量更新（task_/subtask_ 事件落树），无固定渲染模板
- 续流 chips 与澄清卡片复用同一事件流承载

---

# 第三部分 · 决策记录与待决事项

## 20. 已锁定决策

| # | 决策 | 结论 |
|---|---|---|
| D1 | 知识库核心思想 | ER 图当知识图谱，三层模型（结构/语义/行为） |
| D2 | 图数据模型 | 表节点 + 列对列表边（多重图），边按列对区分，绝不按表对去重 |
| D3 | 列节点 | 懒晋升——有边或有标签才成为节点 |
| D4 | 双写架构 | 图管结构、知识库管语义，共用 (表,列) 身份 |
| D5 | 概念字典 | 概念 + 规范枚举 + 成员列值映射，人工确认 + 采样防漂移 |
| D6 | 意图识别 | 任务分解 + 三轴剖面（action 枚举 / modality 有序枚举 / trust 派生） |
| D7 | 引擎 | Plan-and-Execute + ReAct；一任务一 skill；skill 不调 skill |
| D8 | skill | 声明式配置（数据非代码），7 个，路由查表 + general 兜底 |
| D9 | 安全 | 横切 + 防御纵深（skill 白名单 + 全局闸门），写永不自动执行 |
| D10 | 复合请求 | 分解为主路径，失败即停，顺序执行，共享 Context |
| D11 | 构建流程 | 确定性结构层（无 LLM）与 AI 语义层（LLM→draft）分离，图不靠 AI 造；一切边经人工确认才生效（2026-08-31） |
| D12 | 边模型 | 列对列表（复合键）+ 守卫边（多态）+ 自环 + confidence/provenance；构建来源三类（FK/命名/LLM），值重叠与判别器检测删除（2026-08-31） |
| D13 | 表级过滤器 | 定义在 L2、消费在 context 提示（引擎不改写 SQL、不强制注入；引擎只管校验拦截/安全审计/执行）；软删除表级、租户连接级默认+表级豁免 |
| D15 | 追问/续流 | 统一 continuation gate（§19.5）：四种 type 分流；new_question 走完整意图分解，其余续旧上下文；协议层守 new/continuation 边界（2026-09 草案） |
| D16 | 意图澄清 | decompose 新增 clarify 通道（§13.4）：意图不完整/不清晰时执行前刹停发澄清卡片，回答后重新分解；接入 continuation gate（2026-09 草案） |
| D17 | 实时任务流 | 前端任务流改纯事件驱动动态树（§19.6）：替换 STEP_DEFS 静态步槽，复用 task_/subtask_ 事件，无场景固定模板（2026-09 草案） |
| D14 | 会话变量 | 运行时变量（租户/用户/时间）用占位符，执行层替换，不进知识库 |

## 21. 待决事项（后续逐项细化）

| # | 问题 | 影响 |
|---|---|---|
| Q1 | ~~值重叠探测的具体算法~~（2026-08-31 值重叠检测已删，随 §6 简化） | - |
| Q2 | action/modality 各自用规则还是 LLM 判断（快判 vs 判断的边界） | 意图识别成本与精度 |
| Q3 | 任务分解的精确边界（何时拆、何时不拆的判定规则） | 复合请求正确性 |
| Q4 | 图校验（plausibility gate）的判定规则细节（找不到路径时打回 vs 放行阈值） | 多表准确率 |
| Q5 | 概念字典与语义层存储的落库 schema | 存储层设计 |
| Q6 | 各 skill 的 system_prompt 具体措辞 | 场景指导质量 |
| Q7 | 非等值/范围 join（SCD）是否 v2 建模 | 数仓场景覆盖 |
| Q8 | ~~守卫边/多态检测的触发条件与分组采样阈值~~（2026-08-31 检测已删，守卫边由 LLM 提案 + 人工确认） | - |
| Q9 | 会话变量的默认值来源（连接级 vs 全局）与执行替换时机 | 多租户正确性 |

## 22. 参考

- [Text-to-SQL with Knowledge Graphs: Multi-Hop Queries（FalkorDB）](https://www.falkordb.com/blog/text-to-sql-knowledge-graphs/)
- [Domain-specific SQL generation with LLMs: hybrid KG + RAG framework](https://www.sciencedirect.com/science/article/abs/pii/S1474034626002521)
- [sqlmind：property graph schema 的 SQL intelligence layer](https://github.com/veloce-ai/sqlmind)
- [基于 Graphiti 实现 NL2SQL](https://cloud.tencent.cn/developer/article/2689741)
- [A systematic survey of LLM-based text-to-SQL](https://peerj.com/articles/cs-3773/)
- [Agentic-SQL：LLM Text-to-SQL 自主性分类法](https://github.com/suyiyun/llm-text2sql-taxonomy)
