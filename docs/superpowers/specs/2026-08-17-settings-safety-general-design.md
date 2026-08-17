# 系统设置「安全参数 / 通用」分区打磨 — 设计文档

- 日期：2026-08-17
- 状态：已与用户确认设计，待评审
- 目标：将系统设置里的「安全参数」与「通用」两个分区从占位（`defaultValue` 写死、不落盘、不改后端）改为真功能 / 真展示。

## 范围

### 做（In scope）
1. **查询行数上限** → 真设置 `query_max_rows`（默认 1000）。
2. **连接池大小** → 真设置 `pool_size`（默认 3，仅 PG/MySQL 生效；SQLite 恒 1）。
3. **通用区三项只读值** → 改为后端返回的**动态真值**（数据目录 / 监听端口 / 鉴权方式），不再硬编码 `8765` / `~/.cleared`。

### 不做（Out of scope）
- **DML 确认** 下拉项 → 收敛移除（闸门是模型无关纯规则、且不读任何阈值；本次不碰 `app/safety` 判定逻辑）。
- **主题/外观** 分区 → 未来仅加几个配色预设，不做定制主题（已与用户确认）。
- 报告（report）模式链路 → 产品核心功能，不在本次打磨范围。
- `app/safety` 闸门判定逻辑本身 → 保持现状（INERT 阈值不变）。

## 后端改动

### `app/core/settings.py` — `RuntimeSettings`
- 新增字段 `query_max_rows: int = 1000`、`pool_size: int = 3`。
- 加入 `_PERSISTED_KEYS`，使其可经 `PUT /settings` 持久化（`settings.json`）。
- 默认值来源保持 `app/config.py` 的 `CLEARED_QUERY_MAX_ROWS` / `CLEARED_POOL_SIZE` 作初始值（首次启动从 env 读入）。

### `app/core/query.py`
- 读行数上限处由 `get_env().query_max_rows` 改为 `state.runtime.get().query_max_rows`（保留 LIMIT 注入逻辑不变）。

### `app/core/pool.py` — `PoolManager`
- `_pool_for` 创建池时，PG/MySQL 的 `size` 由 `get_env().pool_size` 改为 `state.runtime.get().pool_size`（SQLite 仍恒 1）。
- 新增 `rebuild()`：关闭并清空所有 `_pools`，使设置变更后下次取池时用新大小。
- 在 `PUT /settings` 处理链中，保存成功后调用 `state.pools.rebuild()`（仅当 pool_size 实际变化时可无条件调用，池为空时为空操作）。

### 运行时真值端点
- 复用 `GET /api/v1/settings`：在 `SettingsPublic` 增加 `runtime` 段，返回 `{ data_dir, port, auth }`。
  - `data_dir` / `port` 来自 `app/config.py` 的 `CLEARED_DATA_DIR` / `CLEARED_PORT`。
  - `auth` 固定为 `"X-Cleared-Token · 本机"`。
- 不新增端点，避免分散真值来源。

## 前端改动

### `src/shared/types.ts` — `SettingsPublic`
- 增 `query_max_rows: number`、`pool_size: number`。
- 增 `runtime?: { data_dir: string; port: number; auth: string }`。

### `src/renderer/src/components/SettingsDrawer.tsx`
- **安全参数** 区：
  - 「查询行数上限」改为受控 `input`，值来自 `settings.query_max_rows`，`onChange` 更新本地编辑态，`handleSaveAll` 随模型一起落盘。
  - 「连接池大小」改为受控 `input`，值来自 `settings.pool_size`（保留 hint：SQLite 固定 1）。
  - **移除**「DML 确认」`select` 行（占位、不接闸门）。
- **通用** 区：
  - 三项只读 `input` 值改为来自 `settings.runtime?.data_dir / port / auth`，不再写死。

### `src/renderer/src/api/settings.ts`
- `updateSettings` 的入参类型补充 `query_max_rows` / `pool_size`；`getSettings` 返回类型补 `runtime`。

## 数据流
1. 用户修改两数值 → 前端本地编辑态 → `PUT /settings`（与现有 `ai_models` 同批次）。
2. 后端 `updateSettings` 持久化到 `settings.json`，并触发 `state.pools.rebuild()`。
3. `query.py` / `pool.py` 后续执行读取 `state.runtime.get()` 的新值。
4. 通用区：打开设置时 `GET /settings` 返回 `runtime` 真值 → 只读展示。

## 错误处理
- 数值输入需校验：正整数；非数字 / ≤0 时不写入（前端拦截 + 后端 `updateSettings` 防御性兜底为默认值）。
- `pools.rebuild()` 在有待执行查询的句柄时：当前设计在保存后重建，若恰有进行中查询，句柄归还时关闭；重建仅影响后续 `acquire`，不影响在途请求（池以 `conn_id` 隔离）。

## 测试
- 后端单测：
  - `query_max_rows` 被 `query.py` 读取并注入 LIMIT（边界：恰好 max_rows+1 行被截断）。
  - `pool_size` 被 `pool.py` 读取；`rebuild()` 使新池使用新 size。
  - `PUT /settings` 持久化两字段并触发 rebuild（mock `state.pools`）。
  - `GET /settings` 返回 `runtime` 真值。
- 前端：`npm run typecheck` + `npm run build`。

## 风险 / 注意
- 不触碰 `app/safety` 判定逻辑；闸门保持 INERT 阈值现状。
- SQLite 池恒 1，前端已注明，不受影响。
- 当前工作目录非 git 仓库，spec 文档不提交（用户后续自行管理版本）。
