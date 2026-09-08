# AI 引擎代码审查 · 问题追踪清单

> 起始：2026-09-02 · 覆盖范围：`backend/app/ai/` + 引擎相关 API
> 状态标注：🔴 高 / 🟡 中 / 🟢 低 · `open` = 待处理 · `fixed` = 已修复 · `obsolete` = 随架构演进去向不明/已删
> 处理策略：**问题全部找齐后一并修复**
>
> **2026-09-08 引擎合一批量结案**：chat_stream/意图层（decompose/intent.py）/skill 路由层（skills/、
> api/skills.py、dispatcher）删除，harness 单循环为唯一执行引擎；同轮修复工作台三断点（attachCards/
> 澄清劫持/manifest 缺失）。原清单中引用已删文件的问题标记 obsolete，其余按下列状态更新。

## 已结案（2026-09-08 引擎合一 + 补强轮）

| # | 原问题 | 结案方式 |
|---|---|---|
| H1 | intent.py 常量重复 | obsolete：intent.py 已删（MODE 常量移入 loop.py 本地） |
| H2 | decompose.py 退役未清理 | obsolete：已删 |
| H3 | data_tier.py 重复逻辑 | obsolete：已删 |
| H4 | 两套只读工具集不一致 | fixed：skill 白名单层删除；trust 元数据过滤单一事实来源（`TOOL_SCHEMAS_READONLY`/`tool_schemas(readonly=)` 一并删除） |
| M1 | loop/harness 脱敏管道重复 | fixed：chat_stream 删除，`_prepare` 唯一实现 |
| M2 | 双执行器并存 | fixed：引擎合一，`run_react_loop` 唯一核心 |
| M3 | 卡片骨架丢 options/empty_hint | fixed：`_card_skeleton` 补两字段 |
| M4 | harness include_data 默认与红线冲突 | fixed（政策定版）：设计文档 §16.2#3 修订为 privacy_mode 三档口径（strict 不出行 / standard 封顶+脱敏默认带 / open 同 standard 明文） |
| M5 | propose_plan 确认后工具集不一致 | fixed：`controlled_step_stream` 按 `_STEP_TOOLS` 查表 |
| M6 | suggest_followup 无 ctx 记账 | fixed：补 ctx 走中央记账 |
| M7 | scoped_token/script_analyzer 死代码 | obsolete：已删 |
| M8 | loop.py 头注释过时 | fixed：重写 |
| M9 | suggestions/initial 无 ctx 记账 | fixed：补 ctx |
| M10 | 审批执行绕过 prepare_query_sql | fixed：approve 全链路（闸门评估/回滚剧本/审计/执行）统一用替换后 `exec_sql` |
| M11 | 受控计划工具面不一致 | fixed：同 M5 |
| M12 | _user_id 取值 | obsolete：代码本就用 getattr（过时记录） |
| M22 | strict+report 空叙述 | fixed：`_llm_narration` 加降级块（行未出网 → 定性描述禁止编数字） |
| L2 | intent.py 职责混淆 | obsolete：已删 |

## 存量 open（按优先级）

### 🔴 H6 — open
- **位置**: `api/tasks.py:30-31` 任务创作 Agent 工具白名单模块级初始化（import 顺序依赖）
- **建议**: 惰性初始化（调用时构建）。

### 🟡 M13 — open
- **位置**: `api/suggestions.py` 重复组装 schema 摘要（与 loop 入口重复；30s 缓存兜底）
- **建议**: 复用缓存摘要，低优先级。

### 🟡 M14 — open
- **位置**: `safety/gate.py` 与 `core/query.py` 方言映射两份副本
- **建议**: query.py 直接引用 gate.py 导出的公共函数。

### 🟡 M15 — open
- **位置**: `safety/rules.py` RULE_META 的 floor 字段无消费方（仅靠阶梯逻辑巧合生效）
- **建议**: normalize_gate_rules 显式消费 floor，或注释明确其元数据定位。

### 🟡 M16 — open
- **位置**: `safety/gate.py` preview_rows（COUNT 预估）无独立审计条目（依赖调用方各写）
- **建议**: preview 落一条轻审计。

### 🟡 M17 — open
- **位置**: `safety/codify.py` 代号空间仅 100（`t_{hash%100}`），>50 探测即放弃；敏感表 100+ 会错乱
- **建议**: 扩大空间（四位哈希）或确定性递增编号。

### 🟡 M18 — open
- **位置**: `safety/codify.py` decodify 顺序（先列后表 + 短码前缀边界）
- **建议**: 先长后短排序替换 + 引号场景测试。

### 🟡 M19 — open
- **位置**: `tasks/runner.py` 沙箱仅 macOS 生效（Linux 降级为目录副本隔离）
- **建议**: 文档明确降级；Linux 考虑 bubblewrap/unshare。

### 🟡 M20 — open
- **位置**: `tasks/runner.py` 沙箱网络白名单放行 5432/3306——脚本可绕闸门直连 DB
- **建议**: 只放行 sidecar API 端口。

### 🟡 M21 — open
- **位置**: `safety/confirm.py` token 无执行者身份绑定（单机可接受）
- **建议**: 多用户部署前必须绑用户。

### 🟢 L3/L5/L6/L7/L8/L9/L10 — open
- harness/context 多处 `except Exception: pass`（吞异常，靠日志兜底）；L5 沙箱 TABLETALK_TEST 环境变量隐式依赖；L6 脚本凭据文档；L7 `[DONE]` 与 finally 顺序；L8 facade property hack（已知技术债）；L9 store 每操作新建 sqlite 连接；L10 scheduler tick 无统一失败跟踪。
- 均为低优先级，维持现状可接受。

## 汇总

| 状态 | 数量 |
|------|------|
| 已结案（fixed/obsolete） | 18 |
| 存量 open | 16（🔴1 / 🟡9 / 🟢6） |
