# 安全与审计页重构 · 设计文档

日期：2026-08-26 · 状态：已与用户逐节确认（§1 布局 / §2 后端 / §3 前端均获批准）

## 0. 摘要

把现有"五视图 + 左栏规则"的安全与审计页重构为**异常驱动的主从操作台**：
一行结论开门见山，左列"总"（三档统计 + 可搜索列表），右列"详"（今日时间线 /
选中详情）。同时激活审批链（`approval_id`/`rollback_ref` 落地为真流程），
并把安全信号外提为工作台状态栏常驻徽章。

## 1. 背景与已确认决策

现状问题：AuditPage 五个平级视图（异常/全部流水/按报告/出网/周报）+ 左栏静态
规则区，信息无主次；出网/周报忽略时间范围；SQL 搜索是前端 1000 条窗口内过滤；
审计数据无法在页面之外被感知。

与用户逐项确认的决策：

| 决策点 | 结论 |
|---|---|
| 第一受众 | 开发者本人，异常驱动的自查操作台（方向 A） |
| 打开时刻 | 定期扫一眼 + 事后追查 + **不常主动打开** → 必须"开门即结论"，且信号要外提 |
| 信号外提 | 要。工作台底部 statusline 常驻徽章 |
| 改造边界 | 全栈可动（含审计数据模型、审批/回滚链） |
| 审批链 | 本轮做成真流程；回滚仅留引用桩不执行 |
| 统计图 | 要，三档：今日 / 近7天 / 近30天 |
| 布局 | 左右 50/50 主从；搜索框在左列列表上方 |
| 列表能力 | 服务端 LIKE 搜索（300ms 防抖）+ keyset 游标懒加载 |
| 审批入口 | 主入口 = AiRail 卡片当场批；兜底 = 审计页批准/拒绝；历史永远只读 |

## 2. 信息架构（定稿）

```
┌──────────────────────────────────────────────────────────┐
│ ⚠ 今日 3 拦截 · 1 待确认 [1未读]   最近危险动作…  │ 报表·闸门规则链接 │
├────────────────────────────┬─────────────────────────────┤
│ 左 50% · 总                 │ 右 50% · 详                  │
│  StatsPanel                │  今日时间线（默认）            │
│   三档 pill[今日|近7天|近30天]│   每条: 时间+判定徽章+SQL摘要   │
│   SVG 柱图(小时/天粒度)+关键数│   待确认行内联[批准][拒绝]     │
│  搜索框(常驻) + 判定过滤      │   下拉加载更早(当日)          │
│  子页签[异常待办|全部流水]     │  ── 点左列任一行切换 ↓ ──     │
│  列表(keyset 懒加载)         │  选中详情                    │
│   异常待办: 未读优先,已处理沉底│   SQL全文/触发规则/影响预览    │
│   全部流水: 全量可检索        │   状态/处置按钮               │
└────────────────────────────┴─────────────────────────────┘
```

要点：

- **异常待办子页签不做搜索框**——它是"待办"不是"档案"，只留「只看未读」过滤；
  历史异常去全部流水里搜。
- 「报表 · 出网/周报」「闸门规则 ?」收进顶栏右侧链接，以抽屉/弹层展开；
  现有左栏三层级规则卡降级为该抽屉内容。
- 徽章（信号外提）：statusline 常驻 `[● N 未读异常] [◔ N 待确认]`，
  无事显示 `✓ 今日平安`；点击跳转本页并定位到对应子页签。

## 3. 审批交互模型（定稿）

```
AI 生成 DML → AiRail SQL 卡片(影响行数预览)
  A. 当场批: "批准并执行" → 创建审批 + 批准 + 执行一气呵成
  B. 先放着: 卡片留在 pending 池 → 徽章计数
       ↓ 之后任何时候（工作台或审计页）
     decide: 批准(此刻才真正执行) / 拒绝
```

安全不变量：

1. **批准 ≠ 放行凭证**。decide 时后端对 SQL **重跑安全闸门**；若此刻判为
   BLOCK → 拒绝执行，审批置 `failed`。
2. **一条审批只能被决定一次**。decide 幂等，二次决定返回 409；AiRail 卡片与
   审计页调同一 API，任一侧决定后另一侧自动同步。
3. 动作分三层：轻=标记已处理（纯 triage 元数据）；中=批准/拒绝（pending only）；
   零=历史记录永远只读。

## 4. 后端设计

### 4.1 数据模型（均在 `data_dir/audit.db`）

新表 `approvals`：

```sql
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,              -- uuid
  connection_id TEXT NOT NULL,
  sql_text TEXT NOT NULL,
  sql_hash TEXT NOT NULL,           -- sha256，decide 校验用
  origin_tier TEXT NOT NULL,        -- 创建时快照
  preview_rows INTEGER,
  status TEXT NOT NULL,             -- pending | executed | rejected | failed
                                    -- (approved 为瞬时中间态，随即转 executed 或 failed)
  created_ts TEXT NOT NULL,
  decided_ts TEXT,
  decided_by TEXT,                  -- 本地用户名（单用户）
  executed_audit_id INTEGER,        -- 执行后回填 audit_log.id
  rollback_ref TEXT                 -- 本轮仅存撤销提示桩，不执行
);
```

新表 `audit_ack`（主账本保持 append-only，triage 状态独立存放）：

```sql
CREATE TABLE IF NOT EXISTS audit_ack (
  audit_id INTEGER PRIMARY KEY REFERENCES audit_log(id),
  state TEXT NOT NULL,              -- unread|ack
  acked_ts TEXT
);
```

### 4.2 API

| 端点 | 说明 |
|---|---|
| `GET /api/v1/audit/signal` | 徽章轮询：`{unread_exceptions, pending_approvals, today:{blocked, review}}`，极轻量 |
| `GET /api/v1/approvals?status=pending` | 待办列表（审计页队列数据源） |
| `POST /api/v1/approvals` | 创建审批；请求带 `{connection_id, sql}`，服务端算 hash 与预览 |
| `POST /api/v1/approvals/{id}/decide` | `{decision: approved\|rejected}`；approved → 重跑闸门 → 执行 → 回填 audit_id；幂等，非 pending 返 409 |
| `GET /api/v1/audit/stats?granularity=hour\|day&days=N` | SQL 级 GROUP BY 时间桶，返回 `{bucket_ts, total, allow, review, block}[]`，前端聚合三档 |
| `GET /api/v1/audit` 增强 | 新增 `q`(SQL LIKE)、`cursor`(keyset by id)；废弃全量内存切片；保留 verdict/origin/tier/from_ts/to_ts |
| `GET /audit/egress` `/audit/weekly` | 补 `from_ts`/`to_ts` 参数（修复当前忽略时间范围的缺陷） |

### 4.3 关键语义

- **当场批 = 三合一**：AiRail "确认执行" → `POST /approvals` 创建后立即内部走
  approved 路径执行，一次用户交互完成。
- **keyset 分页**：`cursor` 为上一页最小 id，`WHERE id < ? ORDER BY id DESC LIMIT ?`；
  只增日志下天然稳定。
- **stats**：单条 SQL 按桶聚合（今日=小时桶，7/30 天=天桶），SQLite
  `strftime` 实现；连接过滤沿用现有参数。**时区口径**：时间桶必须与
  `audit_log.ts` 的存储口径一致；若为 UTC 存储，"今日/近N天"的边界需按本地
  时区换算后再过滤，保证前端展示的"今日"与用户感知一致。

> **实现偏差（2026-08-26 校准）**：当场批沿用 query.py 的 confirm_token 一次性凭证通道
> （TOCTOU 防护更完备），不走 approvals；延迟批 = AiRail「转审批」→ 审计页 decide。
> 审批终态沿用 pending/approved/rejected，approved 且带 executed_audit_id 视为已执行。

### 4.4 兼容与迁移

- 两张新表 `CREATE TABLE IF NOT EXISTS`，启动时惰性建表；旧库零迁移。
- `GET /audit` 的 limit/offset 行为保留但前端全面切游标。
- 导航文案「安全与审计」不动，`frontend/scripts/verify-egress.mjs` 选择器不受影响。

## 5. 前端设计

### 5.1 组件结构（替换 AuditPage.tsx）

```
store/auditSignal.ts        zustand: 徽章数字 + 定时器; export refresh()
components/AuditPage.tsx    重写: ConclusionBar + ListPane + DetailPane
components/AuditBadge.tsx   statusline 徽章(AppLayout footer 挂载)
  ListPane
   ├─ StatsPanel            三档 pill + 手写 SVG 柱图(无图表库,仓库规矩)
   ├─ AuditSearch           防抖300ms → q 参数; 判定过滤下拉; 仅[全部流水]页签显示
                           （异常待办页签不出现搜索框，只留未读过滤）
   └─ AuditList             子页签[exception|all]; IntersectionObserver 游标懒加载
  DetailPane
   ├─ TodayTimeline         默认态; 当日下拉加载
   └─ EntryDetail           选中态; 处置按钮按条目类型渲染
drawers: ReportDrawer(出网/周报) · RulesDrawer(原三层级规则卡迁入)
```

### 5.2 轮询

`SIGNAL_POLL_INTERVAL` 常量默认 **30_000ms**，用户预期可能调成 10s（改一个数字）。
除定时外：AI 每次 SSE done、任何 decide 成功后都主动 `refresh()`。

### 5.3 i18n 与主题

- 文案全部进 `locales/{zh-CN,en-US}.ts`：新增 `audit.*` 若干 + `approval.*`；
  不允许组件内裸中文/英文串。
- 颜色/圆角/阴影只引用 `styles/tokens.css` 变量（`--red-dim`、`--amber` 等），
  适配 `data-theme` dark/light，禁止硬编码色值。

## 6. 错误处理

- signal 轮询失败：静默降级，徽章显示上次值或"—"，不打扰工作台。
- decide 时连接不可用 / 执行失败：`approval.status=failed`，失败写入审计
  （verdict 口径复用现有 executed/failed 记录方式），前端 toast 具体原因。
- 双重决定（409）：提示"该审批已在别处被处置"并刷新两侧状态。
- approve 时闸门重判为 BLOCK：前端明确展示拦截原因，引导去流水看详情。

## 7. 测试计划

后端 pytest（新增）：

- approvals 生命周期：创建→批准→执行→executed_audit_id 回填；拒绝路径
- decide 幂等：二次决定 409
- decide 时重跑闸门：SQL 被新规则命中 BLOCK → status=failed 且不执行
- `GET /audit` q 搜索 + cursor 分页正确性与稳定性
- stats 小时/天桶聚合数值正确
- audit_ack 读写 + signal 计数正确
- egress/weekly 时间范围过滤生效

前端验证：

- `npm run typecheck && npm run build`
- `verify-egress.mjs` 回归通过
- 手动验收：dark/light 双主题过一遍；zh/en 切换无裸串；徽章点击定位；
  忽略卡片→审计页补批全链路

## 8. Non-goals（本轮不做）

- 回滚真实执行（rollback_ref 仅存撤销提示文本）
- 审计主账本的任何修改/删除（append-only 底线）
- 引入图表库（SVG 手写柱状图足够）
- 多用户/角色权限（decided_by 取本地登录名，单用户语义）
- 报表导出格式扩展（保留现有 JSONL 导出即可）
