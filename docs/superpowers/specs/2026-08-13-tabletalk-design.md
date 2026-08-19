# TABLETALK — AI 数据库客户端设计文档

日期：2026-08-13 · 状态：后端已实现并全测通过（116 个 pytest），前端已 Web 化——纯 Vite React SPA，由后端 FastAPI 同源托管（2026-08-15 起产品形态为浏览器访问的自托管服务，非桌面 APP）

## 1. 产品定位与差异化

AI 优先的桌面数据库客户端，面向**开发者**（第一优先级人群）。杀手锏：自然语言 → SQL，背后是**本地、模型无关的安全闸门**。

市场竞争判断：text-to-SQL 正在被大厂标配化（BigQuery/Snowflake/AWS Q），拼"AI 更聪明"赢不了。差异化在**信任**：

- "AI 能写，但永远写不危险" —— 安全闸门是产品属性，不是 AI 功能
- "数据默认不出内网" —— 隐私红线 + AI 网关可配置（云端/本地/私有）

用户场景特征：**查多写少**。读是白名单直通（体验优先），写才过闸门（安全优先）。产品口号："ask your database — nothing runs until it's tabletalk."

## 2. 总体架构

React SPA 前端（已建）→ HTTP 调本地 FastAPI sidecar（已建，端口 8765，绑定 127.0.0.1）；生产由后端同源托管 `frontend/dist/`。

sidecar 持有**全部**数据库连接、安全闸门、AI 编排。选择理由：
1. 复用团队 React + FastAPI 技能栈
2. 安全闸门可独立 pytest 测试（纯逻辑模块）
3. 同一服务未来可演进为团队/私有网关服务器（审计、共享 AI 代理）—— 一条代码路径，两种部署形态

```
backend/app/
  main.py          FastAPI 入口 + CORS + 方言导入注册
  state.py         AppState 单例容器
  config.py        env 级配置；reset_env() 供测试
  api/             health / connections / schema / query / audit / settings / ai / knowledge
  core/
    connections.py ConnectionRegistry（JSON 持久化，统一 dialect 字段，credential_ref 预留钥匙串）
    pool.py        PoolManager：每连接懒加载 + 锁；run(fn) 失败自动重连一次
    schema.py      方言感知 schema 发现 + summarize + DDL 导出 + 预览；30s 缓存
    query.py       执行 / 序列化 / 行数上限 / limit-offset / count_total / cancel
    dialects/      DialectAdapter ABC + registry + sqlite/postgres/mysql
  safety/          ★ 安全闸门 —— 纯逻辑、模型无关、全单测覆盖
  ai/              gateway（OpenAI 兼容 + mock）/ context / tools（4 执行工具 + draft_ddl）/ loop（SSE）
  knowledge/       KnowledgeBase：自动 schema 抽取 + 用户标注；关键词检索；retrieve() 预留向量
  audit/           JSONL 审计，每条执行语句一条记录
```

## 3. 安全闸门（核心差异化）

**一个闸门，管所有人**：AI 生成和手动执行的 SQL 走同一套规则（`Origin.AI` / `Origin.MANUAL`）。

三档按来源分流：

| 档 | 来源 | 规则 | 交互 |
|----|------|------|------|
| 读（SELECT/SHOW/EXPLAIN/PRAGMA） | AI + 手动 | 白名单直通；无 LIMIT 提示 | 直接执行 |
| 写（INSERT/UPDATE/DELETE） | AI + 手动 | 无 WHERE 拦截；COUNT 同 WHERE 影响预览；显式确认 | 黄卡确认 |
| DDL（CREATE/ALTER/DROP/TRUNCATE） | **仅手动** | AI 请求 BLOCK（无 DDL 工具，物理不存在）；手动强确认 | 红卡 |

关键不变式：
- **模型无关**：本地规则引擎，模型离线也照常拦截
- **失败封闭**：解析失败/未知语句 → 按写处理（REVIEW），永不 ALLOW
- **白名单读**：READ_TYPES 显式白名单，其余全部非读
- 事务控制（SET/BEGIN/COMMIT）→ 需确认；多语句批处理含非只读 → BLOCK
- confirm 执行前**重新评估**（防 TOCTOU）
- 每条执行语句写审计（语句、判定、时间戳）
- 影响行数预览的 COUNT 需超时保护（P1，见 §10）

**定位声明（对外话术）**：闸门是"防呆"（第二道防线），不是权限系统。第一道防线是数据库层权限 —— `read_only` 连接选项（PG `conn.read_only` / MySQL `SET SESSION TRANSACTION READ ONLY`，已实现）应做成连接向导的显式选项。

## 4. AI 工具集与上下文组装

**工具集 = 4 个执行工具 + 1 个草稿工具，绝无 DDL 执行**：

`get_schema`（结构摘要）· `describe_table`（列定义）· `run_query`（只读、过闸门、默认只回列名+行数）· `run_dml`（过闸门、预览+确认、绝不自动执行）· `draft_ddl`（仅生成脚本，发编辑器手动执行）

上下文（`assemble_context`）：system prompt + 当前连接 schema 摘要 + 选中表 + 知识库检索 + 对话历史。**只发结构（表/列/类型/外键/注释），绝不含行数据。**

## 5. 隐私红线

- **发模型**：schema 结构 + 连接上下文 + 对话历史 + 知识库文档
- **不发**：原始行数据。run_query 默认只回列名+行数；`include_data: true`（请求级 opt-in）才回明细
- **AI 网关可配置**：cloud API 或任意 OpenAI 兼容本地端点（Ollama/vLLM/私有），企业自己决定数据流向
- 待补政策（P1）：schema 本身可能敏感（列名/注释含业务机密）→ 敏感表/列屏蔽清单；对话历史中的 SQL 字面量 PII → 发送前脱敏策略

## 6. 知识库与 schema 检索（图谱 + 向量方向）

**已实现 v4（2026-08-15）—— 核心是"领域标签驱动的检索"，这是产品设计的核心**：

```
构建（每库独立）                        使用
──────────────                        ─────────────
拉全表结构 → 逐表: AI 生成             问题 → LLM 从【已确认标签库】
表描述+领域标签（约束复用现有标签库，     选 1~N 领域标签
新标签提草案） → 逐字段: 业务含义        → 图库: 打该标签的表 + FK 走 2 步
(DDL 注释优先→AI) → 全部可展示可编辑      → 候选子图 → LLM → SQL
标签: draft → 人工确认 → 进入可路由标签库
```

- **标签是"长"出来的**：不预置（预置会让 LLM 误判），由 AI 从真实表结构分解总结，约束在已有标签库内复用（订单表/明细表/退货表共享"订单"）；每库一套，不做跨库。
- **路由只认已确认标签**（不变式）：`route_tables(tags, hops=2)` = 打标签的表 + FK 2 步覆盖 → 候选子图；意图分类 `app/ai/intent.py`（LLM 判定，mock 关键词回退）。
- **AI 上下文按候选子图喂结构**：`assemble_context` 先分类意图→标签→路由→只发相关表结构，避免全表扫描。
- 问题沉淀（高阈值命中历史问题直接复用标签）：**暂不做**，接口预留。
- 注释/图谱/确认/向量检索（v3）保留；存储可插拔（内存模拟默认 / 本机 qdrant·neo4j 可切 / 企业版 Docker），契约测试钉死。
- 隐私：采样只存本地；注释 prompt 默认不含样本值；嵌入默认离线哈希，真语义可切本地 bge-m3；敏感列清单发模型前剔除。

## 7. 多数据源策略

方言注册表已验证：`DialectAdapter` 接口薄（execute/list_tables/list_columns/list_foreign_keys/quote），新增库 = 一个适配器文件 + 注册，核心零改动。sqlglot 自带 oracle/hive/spark 方言，闸门解析可同步扩展。

务实优先级：
1. **MySQL 兼容家族**（OceanBase/PolarDB/TiDB，MySQL 适配器微调即可，性价比最高）
2. **PG 家族**
3. **信创**（达梦/金仓，政务金融必问）
4. **Oracle / SQL Server**（存量最深，驱动与测试最贵）

边界：**不做跨库 JOIN/联邦查询**（ETL 工具的地盘），写入文档。

## 8. 前端交互范式（原型已定）

"数据主场，AI 副驾"：大块面积给结果表格（标签页/过滤/排序/分页/逐列问 AI），右侧窄栏给对话（命令输入 + SQL 卡片 + 安全三色）。安全三色是全局唯一的彩色语言（绿=读/黄=写/红=DDL）。编辑器为独立标签页，与对话双向（选中 SQL → 问 AI）。图谱标签页见 §6。

原型 `design-demo/index.html` 为静态模拟引擎；前端已按 React SPA 落地对接真实 API（API 已全部现成）。

## 9. 设计核对结论（2026-08-13，81 测试全过）

**核对通过**：一闸门管 AI+手动 ✓ 解析失败封闭 ✓ DDL 不进 AI ✓ 无 WHERE 拦截+影响预览 ✓ 隐私红线（列名+行数默认）✓ read_only 会话级只读已实现 ✓ 白名单读 ✓ 多语句含写 BLOCK ✓ confirm 重新评估 ✓。

**发现的缺口**：

| 级别 | 事项 | 状态 |
|------|------|------|
| P0 | sidecar 无鉴权（本机任意进程可调 /query、/settings） | ✅ 已完成（`X-TableTalk-Token` 中间件 + token 文件，87 测试 + 真实 HTTP 冒烟验证） |
| P0 | 行数上限只截断传输不限制 DB 工作（亿行表全量拉回） | ✅ 已完成（`_auto_cap` SQL 层注入 LIMIT cap+1，5 个新测试） |
| P0 | PG/MySQL 适配器零集成测试（"Docker 门控"仅为意图） | ✅ 已完成（tests/integration 8 个测试 + docker-compose.integration.yml，无 docker 自动跳过） |
| P1 | 凭据明文存 JSON（credential_ref 已预留钥匙串） | P2（Web 浏览器场景无原生钥匙串，改 WebCrypto 客户端加密） |
| P1 | preview COUNT 超时保护 | ✅ 已完成（`gate_preview_timeout` 默认 3s，超时返回"无法预估"） |
| P1 | 每连接单连接+单锁全串行 → 小池化/读写分离 | ✅ 已完成（`_ConnPool`：SQLite=1，PG/MySQL 默认 3，并发读不再全串行） |
| P1 | 向量检索 + 图谱扩展（中文桥接） | ✅ 已完成（v2：关键词+哈希向量+FK 平滑；真语义嵌入待接 bge-m3） |
| P2 | 报表 HTML（AI 写模板、数据本地注入，见 §10）、慢查询提示、keyset 分页、信创/Oracle | 待排期 |

## 10. 第二阶段功能：在线生成报表 HTML（已评估，方向批准）

"查多写少"场景的终点能力：把查询变成可分享的洞察。**AI 只写模板（HTML/CSS/SVG 代码），数据本地注入**——AI 需要列名+类型+统计量，不需要明细行，明细数据依然不出内网，与隐私红线完全一致。

产出：自包含单文件 HTML（内联 SVG 图表，不依赖外部 CDN），可打印/邮件/内网分享。

硬约束：① AI 生成的 HTML 必须在沙箱渲染（无权限 iframe + CSP，防 XSS）；② 图表类型收敛（表格+柱/线/饼，不做报表设计器）；③ 注入行数设上限，大结果集先聚合。

## 11. 实施顺序

1. **P0 ✅（2026-08-13 完成）**：sidecar token 鉴权 → 无 LIMIT 读自动注入上限 → docker 门控 PG/MySQL 集成测试
2. **P1 ✅（2026-08-13 完成，除凭据钥匙串）**：连接池化、preview COUNT 超时、知识库 v2（哈希向量+FK 平滑）；凭据钥匙串 P2 改 WebCrypto 客户端加密
3. **P2（待排期）**：真语义嵌入（bge-m3）、报表 HTML、慢查询提示、keyset 分页、多源扩展（MySQL 兼容家族 → 信创 → Oracle/SQL Server）
