# 审计页（Audit Page）重建设计

日期：2026-08-20
状态：方向与 UI 已与用户确认，进入方案文档化
关联：工作台风格已定（tokens.css），知识库/安全闸门/审计为同批"逐页优化"中的第一页

## 1. 核心定位（Core Value）

审计不是"SQL 结果浏览器"，而是产品的**信任证据层**：一张 append-only 的"闸门决策账本"。
产品卖点是"AI 能写，但写不了危险的东西"——审计就是这句话的凭证。

- 对单人开发者：看清 AI agent 对库动了什么念头、有无危险操作被拦下（安心 + 排障）。
- 对企业（后端注释：未来同步团队审计服务器）：合规/SOC2 凭证。

**设计原则：数据是流水账，但页面不以流水账为主角。** 页面呈现"结论/信号"，流水账退为下钻。
采用**异常优先（exception-first）**：用户默认看到的是"需要人看"的条目，而非 99% 放行的读查询。

## 2. 后端数据现实（决定能呈现什么）

`backend/app/audit/logger.py` 每条记录字段：
`ts`(秒级, 无时区) · `connection` · `origin`(ai/manual) · `tier`(read/write/ddl) ·
`verdict`(ALLOW/REVIEW/BLOCK) · `status` · `sql` · `elapsed_ms`(可选) · `report_id`(可选) · `source`(可选)

**硬限制（已知）**：`sql` 仅存**首行且截断 200 字符**（`logger.py:29`）。因此前端永远无法展示完整多行 SQL——这是日志格式限制，非 UI 问题。

现有 `GET /api/v1/audit` 已支持过滤：`connection` / `verdict` / `limit`(≤1000) / `from_ts`~`to_ts` / `report_id`。
**不支持**：`origin`、`tier` 过滤；`offset` 分页（仅尾截 `limit`）。

## 3. 后端增补（用户已确认：合理就该加，不限于现有 API）

以下为合理且低风险的增补，纳入本期：

1. **`GET /api/v1/audit/summary`**（新增）：返回信号区所需聚合，避免把整份日志拉前端算。
   - 返回：`{ total, by_verdict: {allow,review,block,executed}, by_origin: {ai,manual},
     by_tier: {read,write,ddl}, blocked_rate, ai_ddl_count, review_count, window: {from_ts,to_ts} }`
   - 接受与 `/audit` 相同的过滤参数（connection / from_ts / to_ts / verdict），按窗口聚合。
2. **服务端分页**：`GET /api/v1/audit` 增加 `offset`(默认 0) 与返回 `count`(总数)；`limit` 保持上限 1000。
   - 前端分页/虚拟滚动基于 `offset` + `count`，大库不爆渲染。
3. **（可选 / 待定，不在 MVP 强制）完整 SQL 存储**：将 logger 改为保留完整 SQL（或上限提高到如 4000 字符）。
   - 涉及日志体积与潜在敏感行数据，默认不做；若用户要"SQL 取证"再单独立项。
   - MVP 在行展开处明确标注"仅记录首行（后端限制）"，不假装能看全。

> 说明：`origin` / `tier` 过滤若后端不改，前端仍可对已取窗口做客户端过滤；但为一致性，优先用上述 `summary` + `/audit` 参数化。本期前端对"来源/层级"走客户端过滤即可，不强依赖后端改这两个字段。

## 4. UI / 交互设计

沿用工作台已定 tokens（glass 卡、数据 mono、徽章色 allow=teal / review=amber / block=red / executed=blue），不新造风格。

### 4.1 顶部 · 信号区（主角，可点）
四张玻璃卡横排，数字为"该看一眼"的聚合（来自 `/audit/summary`）：
`🔴 被拦截 N` · `⚠ 需确认 N` · `🟠 AI尝试DDL N` · `拦截率 x% / AI来源 y%`
- 卡片带色边（红/琥珀/橙）。
- **点卡片 → 跳到「异常」视图并按该信号过滤**（被拦截→verdict=BLOCK；需确认→verdict=REVIEW；AI尝试DDL→tier=ddl&origin=ai）。

### 4.2 中部 · 三视图切换（segmented control，默认停在「异常」）
`[ 异常 ] [ 全部流水 ] [ 按报告 ]`

- **① 异常（默认）**：只列 BLOCK + REVIEW（"需人看"）。行内快捷过滤 chip：`被拦截 / 需确认 / AI危险`。
  行：`时间 · 徽章 · 层级 · 来源 · SQL首行(截断) · 耗时`。
- **② 全部流水**：时间倒序，基于 `offset`/`count` 分页或虚拟滚动。过滤条：
  `[判定▾] [来源 ai/manual▾] [层级 read/write/ddl▾] [时间范围▾] [SQL搜索…]`
  （判定+时间走后端；来源+层级+SQL 前端对窗口过滤）。
- **③ 按报告**：按 `report_id` 归组，每组 `报告标识 · N条 · 判定分布小徽章`；展开看该报告下查询。
  "报告标识"优先显示可读标题，**实现时确认 report_id 与工作台 report tab 的映射**：若能定位则给「打开报告」跳转，
  否则展示 `report_id` 并提供复制（不假装能跳）。此为已知小歧义，实现阶段核对。

### 4.3 行交互（统一，内联展开不弹窗）
点任意行 → 原地下拉展开：
```
├─ 14:23:01  🔴BLOCK  ddl  ai
│   SQL: CREATE TABLE tmp ...（首行，≤200字）
│   元数据: tier=ddl · origin=ai · 0.4ms · report=rep_8a
│   ⓘ 仅记录首行（后端限制）   [在报告中查看→](有 report_id 且可定位时；否则仅展示/复制 report_id)
```
展开含"为什么被拦"一句规则说明（如"BLOCK：AI 的 DDL 工具不可执行"），源自 verdict/tier 映射。

### 4.4 全局控件
- **时间范围**下拉：今天 / 近7天 / 近30天 / 全部 → `from_ts~to_ts`。
- **导出**：当前过滤结果 → 下载 JSONL（前端拼，零后端改动）。
- **状态**：加载中骨架；异常视图"无异常 🎉"空态；错误走 toast/横幅。

### 4.5 组件复用
- 与 `GatePage` 共用"判定行 / 徽章"小组件（`Badge` 现定义在 `ModulePages.tsx`，提取为 `components/VerdictBadge.tsx` 或直接复用），消掉重复。
- 信号卡可抽 `StatCard` 小组件。

## 5. 范围

**做**：信号区(4 卡) + 三视图(异常/流水/报告) + 行内联展开 + 过滤(判定/来源/层级/时间/SQL) + 分页 + 导出 + 加载/空/错态 + 与 GatePage 复用徽章 + 后端 `summary` 与 `offset` 分页。
**不做（本期）**：完整 SQL 存储取证（见 3.3，可选待定）；服务端 `origin`/`tier` 过滤参数（前端过滤兜底）；实时推送（轮询/WS 不在范围）。

## 6. 验证
- 后端：`.venv/bin/python -m pytest -q`（新增 `summary` + `offset` 分页单测；现有 audit logger 测试不受影响）。
- 前端：`npm run typecheck && npm run build`。
- 手动：起后端 + `npm run build` + 打开 `http://127.0.0.1:8765` → 审计页；用演示库跑几条 AI 查询（含一次制造 BLOCK，如让 AI 出 DDL 或 DELETE 无 WHERE），确认：信号区计数正确、异常视图默认命中、点卡片跳转过滤、行展开显示元数据、导出 JSONL、空态/加载态正常。

## 7. 不在本期（后续页）
安全闸门加"SQL 过闸模拟器"、知识库标签描述编辑/手动建标签/删除合并、工作台 AI 栏推理步骤去 mock——各自独立立项。
