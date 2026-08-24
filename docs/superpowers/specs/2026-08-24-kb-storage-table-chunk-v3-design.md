# 知识库存储与工作台重构（v3 架构）— 设计文档

- 日期：2026-08-24
- 状态：已冻结（用户逐项确认），执行中
- 前置：分支 `feature/kb-build-flow` 上已完成门禁常驻胶囊/弹窗受控化/按钮收敛/审计留痕/日志加固（19 提交）；本 spec 是其上的存储层重构

## 1. 决策背景（用户驱动）

1. 枚举不应是独立环节/独立存储——解析表时（授权带样本）一次性产出列注释 + 取值对照 + 示例值，
   全部挂载在"表和字段的理解"上
2. 向量库要高可读：**只有一种类型 = table**，一表一 chunk；embedding text 是自然语言表描述；
   payload 存结构化信息（DDL、标签、布局坐标等）
3. 存储层推倒重建为按表组织（非按列碎片），审阅交互保留逐列 ✓/✕
4. 审阅工作台三列 = 领域标签 | 按表内容块 | **2D 可编辑关系图**（拍平版，区别于工作台 3D 浏览图）
5. 边模型：字段级端点 + 基数类型（n:1 / 1:1）；from 恒为多侧；无箭头；n:m 不落单边

## 2. 新存储模型（快照 v2，旧工件作废不迁移）

```python
ColumnInfo:
  name: str; type: str; pk: bool; fk: bool
  db_comment: str          # 数据库自带注释（参考源）
  comment: str             # 业务含义（AI 生成 → 人工确认）
  values: str              # 可选值对照 "P=待付款; S=已发货; R=已退货"
  example: str             # 示例值（主键倒序抽样首个非空值，截断60字符）
  status: str              # none | draft | confirmed（comment+values 整体确认）

TableKnowledge:
  name: str; db_comment: str; column_count: int
  comment: str; status: str        # 表级 AI 注释及状态
  columns: dict[str, ColumnInfo]
  ddl: str                 # CREATE TABLE 文本 → 进 payload
  excluded: bool           # 图谱排除（沿用）

连接级：tables: dict[name→TableKnowledge] + tags 标签库 + graph edges（后两者独立存储，不在 chunk 内）
```

- 快照版本号写入 meta；读到 v1 工件直接视为空库（日志提示重新构建）

## 3. 生成流水线（四阶段 → 三阶段）

| 阶段 | 内容 |
|---|---|
| 一 · AI 正在处理 | 逐表注释（授权时带样本）：一次产出每列 comment + values + 表注释 → 写 ColumnInfo/TableKnowledge（draft）。values 由 LLM JSON 可选字段提供，后端规范化 |
| 二 · AI 标签提取 | 不变（领域标签 draft） |
| 三 · AI 关系识别 | 不变 + 边 v2 方向规范（§5） |

- **枚举独立阶段彻底移除**：jobs.PHASES 回三条；`annotate_enums_core/_parse_enum_items/_mock_enums/_distinct_enum_values` 及 `/enums/*`、`/annotate-enums` API 全删
- 授权门控语义不变：未授权 → 无样本 → comment 仅凭结构、values/example 为空

## 4. 向量合一（一表一 chunk）

```text
【embedding text】orders，订单表。字段有：id：主键，订单ID，示例为123；
status：订单状态，可选值：S=已发货、R=已退货；amount：订单金额…

【payload】{ "ddl": "CREATE TABLE…", "tags": ["交易"], "layout": {...},
             "draft_count": 0, "updated_at": "…" }
```

- 合成规则：confirmed 内容优先；draft 在 payload 记 `draft_count`（AI 使用要求 ready，正常态无 draft）
- **doc_vec 与 table_vec 两套向量合并**为单一表级 chunk 集；`retrieve/to_context/route_tables/vector_route_tables/kb_read` 全部消费表级知识卡
- 未授权构建 → chunk 无示例值和可选值（仅结构描述），口径统一
- 布局坐标持久化在 payload.layout（前端拖拽写回）

## 5. 边模型 v2

```python
GraphEdgeV2:
  from_table, from_col   # 多侧端点（字段级）
  to_table, to_col       # 一侧端点
  cardinality            # "n:1" | "1:1"
  kind                   # "fk" | "llm" | "user"
  status                 # draft | confirmed
  reason                 # 推断依据（悬停展示）
```

方向与基数来源：

| 来源 | 规则 |
|---|---|
| FK 边 | FK 子表=from(多侧)→被引用父表=to；FK 兼 PK → 1:1，否则 n:1（unique 索引探测为后续增强，第一版近似） |
| LLM 边 | prompt 强制四元组+cardinality+reason，from 必须多侧，缺字段/矛盾丢弃；n:m 引导经中间表拆解，不落单边 |
| 手绘边 | 拖线弹面板选两端字段+基数（默认 n:1） |

归一化不变量：from 恒为多侧（1:1 时为 FK 持有方）；无箭头（基数用线型/徽标区分）；
墓碑按字段对记录（不含基数）；2D 图与 3D 工作台流动动画共用同一份边存储。

## 6. 审阅工作台（前端三列）

- 左：领域标签库（现状不动）
- 中：**按表内容块**——绑新模型：表头（表名/表注释/状态）、字段行（名称/类型/PK/FK 徽标/业务注释/可选值/示例 + 逐列草案 ✓/✕，✕ 整列撤下有提示）
- 右：**TableRelationGraph2D**（新核心组件，手写 SVG 不引第三方图库）：
  - 领域聚类初始布局 + 节点拖拽（坐标持久化 payload.layout）
  - 字段级边标签 `orders.customer_id ── n:1 ── customers.id`；无箭头；n:1 实线 / 1:1 双短线徽标
  - 连线增删：拖线弹面板选两端字段+基数；draft 边虚线样式
- 宿主 ×2：审阅弹窗右栏 + 知识库编辑页新增 2D 编辑形态（与 Graph3D 共存切换）
- confirm-all 贯通新模型（确认全部列/表草案 + draft 边 → ready）

## 7. 非目标

- 不迁移旧工件（重新构建即得）；不做 unique 索引探测（近似规则先行）
- 不做 n:m 直连边；不改门禁/审计/构建任务框架
- Graph3D 本身不重做（保持浏览形态）

## 8. 验收要点

1. 重建后审查页中列出现含「可选值」「示例」的字段行（授权构建时）
2. 向量库每连接恰 N_table 条 chunk，embedding text 为可读表描述；payload 含 DDL/tags
3. AI 对话上下文【知识库】段输出命中表的完整知识卡
4. 2D 图：领域聚拢、拖拽持久化、边标签字段级、增删可用、方向与 3D 流动一致
5. 未授权构建的 chunk 无 values/example
6. 全量回归绿 + sidecar 健康
