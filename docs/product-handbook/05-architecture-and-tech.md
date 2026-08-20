# 05 · 技术架构与选型

> 现状基线（已建成并验证）-> 演进设计 -> 选型原则与禁令。
> 改后端结构、加任何依赖之前必读。

## 1. 架构现状（2026-08 基线）

```
浏览器 SPA（React 18 + TS + Vite + Zustand）
  │  同源 /api/v1/*，X-TableTalk-Token（/bootstrap 免鉴权发放）
  ▼
FastAPI sidecar（backend/app/）
  ├─ api/        thin routers：health/connections/schema/query/audit/settings/ai/knowledge
  ├─ core/       连接注册表、运行时设置、连接池、schema 发现(30s缓存)、查询执行(行数上限/LIMIT注入/取消)、方言注册(sqlite/pg/mysql)
  ├─ safety/     ★ 三档闸门：纯函数、模型无关、全单测
  ├─ ai/         gateway(cloud/local/mock) + context(结构不上行数据) + intent + 5 tools + function-calling loop(SSE)
  ├─ knowledge/  KB v4：结构+向量+图谱+注释/标签(草稿->确认)+意图路由（详见 07）
  └─ audit/      JSONL 审计
```

- 前端由后端 StaticFiles 同源托管；开发态 Vite 5173 代理。
- mock 为默认 provider -> 全链路离线确定性可跑（工程纪律 = 商业弹药）。
- 测试基线：116 pytest + 8 docker 门控 PG/MySQL 集成。

## 2. 架构原则（与技术选型同级约束）

1. **sidecar 持有所有连接与守门**：浏览器永不直连数据库、永不做安全裁决。任何"前端顺手校验"只是体验层（C1 lint），**不是防线**。
2. **守门逻辑全部本地纯函数**：闸门、脱敏、代号化，不依赖任何模型在线；输入 SQL/策略 -> 输出裁决，可独立测试（F2 拆库的地基）。
3. **AI 请求单管道**：context 构建 -> 脱敏（B2）-> 清单生成（B1）-> gateway 发送。所有出网内容必须过同一条管道，禁止旁路直连模型。
4. **扩展点在方言注册表与工具集**：新数据库 = 新 `DialectAdapter`；AI 能力边界 = tools 白名单（无 DDL 执行工具这条红线不动）。
5. **连接管理独立于 UI 生命周期**：为 P3 proxy 期权留路（见 §6）。

## 3. 演进设计（按 Phase）

### 3.1 P1：管道插桩（出网清单）

- `ai/context.py` 构建请求时产出 manifest 对象；`gateway.py` 发送前校验"payload == manifest 所述"（pytest 断言一致性）。
- SSE 增加 `manifest` 事件（现有 think/sql_card/text/done 旁）。
- 审计 JSONL：新增事件类型 `egress`；**同时引入 `schema_version` 字段**（为后续字段演进留兼容读旧逻辑）。

### 3.2 P2：脱敏层与三档

- 新增 `app/safety/redact.py`：纯函数 `redact(text|rows, rules) -> redacted + manifest`。与闸门同目录 = 同哲学（本地可信层）。
- 确定性 tokenization：`HMAC-SHA256(value, salt)` 截断 -> `[PHONE_a3f2]` 形式；盐存 `data_dir/redact.key`（chmod 600，同 token 文件管理方式）；映射表存内存 + 惰性持久化（用于结果还原，永不出网）。
- 三档配置进 `RuntimeSettings`（现有 JSON 持久化 + PUT /settings 通路，不新建配置系统）。
- 成本防护：`DialectAdapter` 增加 `explain()`（SQLite `EXPLAIN QUERY PLAN` / PG `EXPLAIN` / MySQL `EXPLAIN`），解析估算行数交闸门升档。**EXPLAIN 失败一律放行并审计降级原因**（可用性优先）。

### 3.3 P2：闸门拆库（tabletalk-gate）

- 把 `app/safety/` 抽为独立 pip 包（纯函数、零三方依赖，sqlglot 作为唯一 peer dependency），主仓库 `requirements.txt` 引之。
- 拆库顺序：先在主仓库内建 `safety/` 的公共接口（裁决输入/输出 dataclass），包只做搬运不改逻辑，116 个测试随包走。
- 攻击测试集：`tests/attack/` YAML 用例（SQL + 期望裁决 + 期望规则 ID），包 CI 跑，README 徽章展示通过率。

### 3.4 P3：多用户与网关

- 鉴权：单机模式保留 bootstrap token（零配置）；团队模式启用 OIDC（Authentik/Keycloak 兼容）或内置账号；中间件按模式二选一。
- 凭证保险库：连接配置从 `connections.json` 平文演进为 AES-GCM 加密（主密钥来自环境变量/密钥文件，部署时注入）；**保留明文模式的迁移工具**（开源用户升级零痛苦）。
- 审计与用量：JSONL 保留为事实源，增加聚合层（SQLite 索引表）支撑报表；全部事件带 `user_id`。
- proxy 期权：连接池与查询执行已与 HTTP 路由解耦（`core/pool.py` + `core/query.py`），未来加协议层（pgbouncer 式）不动核心--**此为不做承诺的期权，仅要求新代码不把 pool 生命周期绑死在 FastAPI app 上**。

## 4. 前端技术约定

- 状态：Zustand（现有），新增 store 遵循现有 slices 模式；**不引入** Redux/TanStack Query 等重型方案。
- 数据获取：现有轻封装 `api/client.ts`（token 注入）持续演进；SSE 用现有事件协议扩展，**事件类型只在 `ai/loop.py` 一处定义**，前端 types.ts 同步生成或对照更新。
- SQL 编辑器选型（C1）：优先 CodeMirror 6（体积小、lint decoration API 完备、SQLite 友好）；Monaco 仅在 CodeMirror 无法满足时再评估（体积与 electron 依赖是负担）。lint 计算：**先做后端 `/sql/lint` 轻端点**（复用后端 sqlglot 与规则，保证与闸门同源），前端防抖调用；性能不达标再考虑 sqlglot wasm 前置。
- 图表（报告模式）：结果摘要优先表格 + sparkline 内联，不引入图表库（反目标：不做 BI；真需要时用轻量 SVG 手绘）。
- i18n：现有自研词典体系，新功能文案先入词典。

## 5. 已知技术债与安全债（做相关功能时必须一并处理）

| # | 债 | 处理时机 |
|---|-----|----------|
| 1 | `TABLETALK_GATE_REVIEW_THRESHOLD` 等配置持久化但无人读取（空转） | A2 策略落地时做实，或先从 settings 响应中移除以免误导 |
| 2 | `cloud` provider 无 key 静默降级 mock，掩盖配置错误 | B4 三档落地时：标准/开放档下缺 key 显式报错；仅严格档允许 mock 回退 |
| 3 | CORS 全开（`allow_origins=["*"]` + credentials）+ `/bootstrap` LAN 可发 token | E4 多用户化时收紧：同源或显式 origin 白名单；单机模式绑定 127.0.0.1（现状默认即是，保持文档声明） |
| 4 | 审计 JSONL 无 schema 版本 | 3.1 加 `schema_version` |
| 5 | 聊天 `run_query` 工具不写审计（仅手动 /query 与报告写） | P1 顺手补：工具调用写审计（标注 origin=ai_session），否则"每一笔都记在审计里"的承诺有漏 |
| 6 | `cloud`+`api_key` 下报告模式强制 include_data=true | B2 脱敏层完成前，报告模式在标准档应明确提示将发送聚合明文（B4 档位联动） |
| 7 | `app/debuglog.py` 调试模块（"TODO: 测试后删除"）在 7 个文件残留引用 | Phase 0 清理，删模块 + 全部引用，跑全量 pytest 确认（KB 侧细节见 07 §9） |
| 8 | CLAUDE.md 的 KB 持久化描述仍写 JSON（已演进为 SQLite 默认） | Phase 0 更新 CLAUDE.md，保持"代码现状"文档准确 |

## 6. 依赖选型原则与禁令

- 原则：能纯函数不引框架；能标准库不引三方；每加一个运行时依赖在 PR 描述里写"为什么没有更轻的选择"。
- 禁令清单（对应 01 反目标）：不上向量数据库（KB 哈希/API 向量够用；换装点与例外条件见 07 §8）、不上图表库、不上消息队列/微服务全家桶（单体 sidecar 是特性不是缺陷）、AI SDK 不深度绑定单一厂商（保持 OpenAI-compatible 协议层）。
- sqlglot 保持核心解析依赖；升级需过全量 safety 测试 + 攻击集。
