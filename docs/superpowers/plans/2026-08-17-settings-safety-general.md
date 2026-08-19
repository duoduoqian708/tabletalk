# 系统设置「安全参数 / 通用」打磨 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把系统设置里的「安全参数」「通用」两个分区从占位（`defaultValue` 写死、不落盘、不改后端）改成真功能 / 真展示：`query_max_rows`、`pool_size` 成为可持久化运行时设置；通用区只读值改为后端动态真值；移除不接闸门的「DML 确认」占位项。

**Architecture:** 后端 `RuntimeSettings` 新增两字段并纳入持久化（`_PERSISTED_KEYS`）；`query.py` / `pool.py` 改读 runtime 设置；`pool.py` 加 `rebuild()` 在设置变更后重建连接池；`GET /settings` 的 `public()` 增加 `runtime` 真值段。前端 `SettingsDrawer` 把两数值改为受控输入并随「全部同步」落盘，通用区只读值改读 `runtime`。

**Tech Stack:** Python 3 / FastAPI / pydantic（后端）；React 18 + TypeScript / Vite（前端）。测试：pytest（后端）、tsc（前端）。

**注意：** 当前工作目录不是 git 仓库，任务里的 `git commit` 步骤可按需跳过（或先 `git init`）。

---

### Task 1: 后端 RuntimeSettings 新增字段并持久化

**Files:**
- Modify: `backend/app/core/settings.py`
  - `RuntimeSettings` 数据类（约 line 66 后）
  - `_PERSISTED_KEYS`（line 179）
  - `public()`（line 126）
  - `SettingsStore.__init__`（line 259）
  - `SettingsStore.get()`（line 340）
  - `SettingsStore.update()` 的「其他字段」循环（line 406）

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/api/test_settings_safety.py`：

```python
"""安全参数 / 通用 运行时设置的持久化与真值端点测试。"""
from __future__ import annotations

import pytest


async def test_settings_persists_query_max_rows_and_pool_size(client):
    r = await client.put("/api/v1/settings", json={"query_max_rows": 50, "pool_size": 7})
    assert r.status_code == 200
    body = r.json()
    assert body["query_max_rows"] == 50
    assert body["pool_size"] == 7


async def test_settings_runtime_section_present(client):
    r = await client.get("/api/v1/settings")
    body = r.json()
    assert "runtime" in body
    assert body["runtime"]["port"] == 8765
    assert body["runtime"]["auth"] == "X-TableTalk-Token · 本机"
    assert isinstance(body["runtime"]["data_dir"], str) and body["runtime"]["data_dir"]
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/bin/python -m pytest tests/api/test_settings_safety.py -q
```

预期：FAIL（`query_max_rows` / `runtime` 未返回）。

- [ ] **Step 3: 在 RuntimeSettings 数据类加字段**

`backend/app/core/settings.py`，在 `kb_ai_annotation_samples` 字段（line 67）后插入：

```python
    # ---- 查询 / 连接池（运行时可覆盖 env 默认值）----
    query_max_rows: int = 1000
    pool_size: int = 3
```

- [ ] **Step 4: `_PERSISTED_KEYS` 加入两字段**

`backend/app/core/settings.py`（line 179 的集合）追加：

```python
    "query_max_rows",
    "pool_size",
```

- [ ] **Step 5: `public()` 增加字段与 runtime 段**

`backend/app/core/settings.py` 的 `public()` 方法，`kb_ai_annotation_samples` 那一行之后、`# 兼容字段` 之前插入：

```python
            "query_max_rows": self.query_max_rows,
            "pool_size": self.pool_size,
            "runtime": {
                "data_dir": str(get_env().data_dir),
                "port": get_env().port,
                "auth": "X-TableTalk-Token · 本机",
            },
```

（`get_env` 已在文件顶部 `from app.config import get_env` 导入。）

- [ ] **Step 6: `SettingsStore.__init__` 用 env 初始化默认值**

`backend/app/core/settings.py`（line 259 的 `self._data` 字典）追加：

```python
            "query_max_rows": env.query_max_rows,
            "pool_size": env.pool_size,
```

- [ ] **Step 7: `SettingsStore.get()` 透传两字段**

`backend/app/core/settings.py` 的 `get()` 方法 `RuntimeSettings(...)` 调用里、`kb_ai_annotation_samples=...` 之后加：

```python
            query_max_rows=data.get("query_max_rows", 1000),
            pool_size=data.get("pool_size", 3),
```

- [ ] **Step 8: `update()` 的「其他字段」循环纳入两字段（含正整数防御）**

`backend/app/core/settings.py`（line 406）的循环改为：

```python
            for k in ("gate_review_threshold", "gate_rules",
                      "kb_sample_rows", "kb_ai_annotation_samples",
                      "query_max_rows", "pool_size"):
                if k in patch:
                    v = patch[k]
                    if k in ("query_max_rows", "pool_size"):
                        # 防御：非正整数不写入，回退默认值
                        try:
                            v = max(1, int(v))
                        except (TypeError, ValueError):
                            continue
                    self._data[k] = v
```

- [ ] **Step 9: 运行测试确认通过**

```bash
cd backend && .venv/bin/python -m pytest tests/api/test_settings_safety.py -q
```

预期：PASS。

- [ ] **Step 10: 提交（可选）**

```bash
cd backend && git add app/core/settings.py tests/api/test_settings_safety.py && git commit -m "feat(settings): add query_max_rows/pool_size runtime fields + runtime info"
```

---

### Task 2: 后端 pool.py 读 runtime pool_size + rebuild()

**Files:**
- Modify: `backend/app/core/pool.py`（`_pool_for` line 75、`PoolManager` 类）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/api/test_settings_safety.py` 追加：

```python
async def test_pool_for_reads_runtime_pool_size(monkeypatch):
    from app.core.pool import PoolManager
    from app.core.settings import RuntimeSettings

    class FakeRegistry:
        def get(self, conn_id):
            class Cfg:
                dialect = "postgres"
            return Cfg()

    rs = RuntimeSettings(pool_size=9)

    class FakeRuntime:
        def get(self):
            return rs

    class FakeState:
        runtime = FakeRuntime()

    monkeypatch.setattr("app.state.get_state", lambda: FakeState())
    pm = PoolManager(FakeRegistry())
    pool = pm._pool_for("c1")
    assert pool.max_size == 9


async def test_pool_rebuild_clears_pools(monkeypatch):
    from app.core.pool import PoolManager

    class FakeRegistry:
        def get(self, conn_id):
            class Cfg:
                dialect = "postgres"
            return Cfg()

    class FakeRuntime:
        def get(self):
            from app.core.settings import RuntimeSettings
            return RuntimeSettings(pool_size=3)

    class FakeState:
        runtime = FakeRuntime()

    monkeypatch.setattr("app.state.get_state", lambda: FakeState())
    pm = PoolManager(FakeRegistry())
    pm._pool_for("c1")
    assert "c1" in pm._pools
    await pm.rebuild()
    assert pm._pools == {}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && .venv/bin/python -m pytest tests/api/test_settings_safety.py::test_pool_for_reads_runtime_pool_size tests/api/test_settings_safety.py::test_pool_rebuild_clears_pools -q
```

预期：FAIL（`max_size` 仍为 env 默认 3；`rebuild` 不存在）。

- [ ] **Step 3: `_pool_for` 读 runtime，并加 `rebuild()`**

`backend/app/core/pool.py` 的 `_pool_for` 改 `size` 计算：

```python
    def _pool_for(self, conn_id: str) -> _ConnPool:
        p = self._pools.get(conn_id)
        if p is None:
            cfg = self._registry.get(conn_id)
            # SQLite 单连接串行（文件锁 + 并发写语义）；PG/MySQL 用运行时可配小池
            size = 1 if cfg.dialect == "sqlite" else self._runtime_pool_size()
            p = _ConnPool(self._registry, conn_id, size)
            self._pools[conn_id] = p
        return p

    def _runtime_pool_size(self) -> int:
        try:
            from app.state import get_state
            return get_state().runtime.get().pool_size
        except Exception:
            return get_env().pool_size
```

在 `close_all` 方法之后给 `PoolManager` 加：

```python
    async def rebuild(self) -> None:
        """设置变更后重建所有池（下次取池时用新 size / 新连接）。"""
        await self.close_all()
```

（`get_env` 已在 pool.py 顶部导入。）

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && .venv/bin/python -m pytest tests/api/test_settings_safety.py -q
```

预期：PASS。

- [ ] **Step 5: 提交（可选）**

```bash
cd backend && git add app/core/pool.py tests/api/test_settings_safety.py && git commit -m "feat(pool): read runtime pool_size + add rebuild()"
```

---

### Task 3: 后端 query.py 用 runtime query_max_rows

**Files:**
- Modify: `backend/app/core/query.py`（line 124）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/api/test_settings_safety.py` 追加（验证 `_auto_cap` 注入 `cap+1`，即行数上限生效的纯逻辑）：

```python
def test_auto_cap_injects_limit_cap_plus_one():
    from app.core.query import _auto_cap
    out = _auto_cap("SELECT * FROM t", "sqlite", 1000)
    assert "LIMIT 1001" in out.upper()
    # 已有 LIMIT 不覆盖
    out2 = _auto_cap("SELECT * FROM t LIMIT 5", "sqlite", 1000)
    assert "LIMIT 5" in out2.upper()
```

- [ ] **Step 2: 运行测试确认通过（`_auto_cap` 已是既有纯函数）**

```bash
cd backend && .venv/bin/python -m pytest tests/api/test_settings_safety.py::test_auto_cap_injects_limit_cap_plus_one -q
```

预期：PASS（此步仅锁定既有 cap 逻辑，下一步改读取来源）。

- [ ] **Step 3: 改读取来源为 runtime**

`backend/app/core/query.py` line 124：

```python
    max_rows = max_rows or state.runtime.get().query_max_rows
```

- [ ] **Step 4: 运行全部本文件测试**

```bash
cd backend && .venv/bin/python -m pytest tests/api/test_settings_safety.py -q
```

预期：PASS。

- [ ] **Step 5: 提交（可选）**

```bash
cd backend && git add app/core/query.py tests/api/test_settings_safety.py && git commit -m "feat(query): use runtime query_max_rows instead of env"
```

---

### Task 4: 后端 api/settings.py 接收字段 + 触发 rebuild + 返回 runtime

**Files:**
- Modify: `backend/app/api/settings.py`（line 14 `SettingsUpdate`、line 44 `update_settings`）

- [ ] **Step 1: 写失败测试（已在 Task 1 覆盖：PUT 后 GET 返回新值 + runtime 段）**

无需新测试；Task 1 的 `test_settings_persists_query_max_rows_and_pool_size` / `test_settings_runtime_section_present` 已覆盖 `SettingsUpdate` 解析与 `public()`。此处额外加一个 rebuild 触发测试：

在 `backend/tests/api/test_settings_safety.py` 追加：

```python
async def test_update_settings_triggers_pool_rebuild(client, monkeypatch):
    # 确保 rebuild 被调用：monkeypatch 后在 PUT 后检查 _pools 已清空
    import app.api.settings as settings_mod
    called = {}

    async def fake_rebuild(self):
        called["rebuild"] = True

    monkeypatch.setattr("app.core.pool.PoolManager.rebuild", fake_rebuild)
    r = await client.put("/api/v1/settings", json={"pool_size": 5})
    assert r.status_code == 200
    assert called.get("rebuild") is True
```

- [ ] **Step 2: 运行确认失败**

```bash
cd backend && .venv/bin/python -m pytest tests/api/test_settings_safety.py::test_update_settings_triggers_pool_rebuild -q
```

预期：FAIL（`rebuild` 未被调用 → `called` 为空）。

- [ ] **Step 3: 加 SettingsUpdate 字段 + 调用 rebuild**

`backend/app/api/settings.py` 的 `SettingsUpdate` 在 `kb_ai_annotation_samples` 之后加：

```python
    # 查询 / 连接池（运行时可覆盖）
    query_max_rows: int | None = None
    pool_size: int | None = None
```

`update_settings` 改为：

```python
@router.put("/settings")
async def update_settings(body: SettingsUpdate) -> dict:
    state = get_state()
    runtime = state.runtime.update(body.model_dump(exclude_none=True))
    await state.pools.rebuild()
    return runtime.public()
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && .venv/bin/python -m pytest tests/api/test_settings_safety.py -q
```

预期：全部 PASS。再跑一遍全量回归确认无破坏：

```bash
cd backend && .venv/bin/python -m pytest -q
```

预期：通过（147 passed 量级，无新增失败）。

- [ ] **Step 5: 提交（可选）**

```bash
cd backend && git add app/api/settings.py tests/api/test_settings_safety.py && git commit -m "feat(settings-api): accept query_max_rows/pool_size, rebuild pools on update"
```

---

### Task 5: 前端类型补充

**Files:**
- Modify: `frontend/src/renderer/src/api/settings.ts`

- [ ] **Step 1: `SettingsPublic` 增加字段**

在 `frontend/src/renderer/src/api/settings.ts` 的 `SettingsPublic`（line 40）中、`kb_ai_annotation_samples` 之后加：

```typescript
  query_max_rows: number
  pool_size: number
  runtime?: { data_dir: string; port: number; auth: string }
```

- [ ] **Step 2: `SettingsPatch` 增加字段**

在 `SettingsPatch`（line 60）末尾、`}` 之前加：

```typescript
  query_max_rows?: number
  pool_size?: number
```

- [ ] **Step 3: typecheck**

```bash
cd frontend && npm run typecheck
```

预期：通过（此时前端尚未使用新字段，仅类型层面）。

- [ ] **Step 4: 提交（可选）**

```bash
cd frontend && git add src/renderer/src/api/settings.ts && git commit -m "feat(settings-types): add query_max_rows/pool_size/runtime"
```

---

### Task 6: 前端 SettingsDrawer 绑定受控输入 + 动态通用值

**Files:**
- Modify: `frontend/src/renderer/src/components/SettingsDrawer.tsx`

- [ ] **Step 1: 加本地状态**

在 `frontend/src/renderer/src/components/SettingsDrawer.tsx`（line 70 `setDefaultEmb` 之后）加：

```tsx
  const [maxRows, setMaxRows] = useState(1000)
  const [poolSize, setPoolSize] = useState(3)
  const [runtime, setRuntime] = useState<{ data_dir: string; port: number; auth: string } | null>(null)
```

- [ ] **Step 2: 加载时填充状态**

在 `useEffect` 的 `.then((s) => { ... })` 中（line 86-89 之后）加：

```tsx
        setMaxRows(s.query_max_rows ?? 1000)
        setPoolSize(s.pool_size ?? 3)
        setRuntime(s.runtime ?? null)
```

- [ ] **Step 3: `persist()` 携带两字段**

在 `frontend/src/renderer/src/components/SettingsDrawer.tsx` 的 `persist()` 函数里（line 233 `await updateSettings({` 的 payload）追加：

```tsx
        query_max_rows: maxRows,
        pool_size: poolSize,
```

（放在 `default_embedding_model: defEmb,` 之后即可。）

- [ ] **Step 4: 安全参数区改为受控 + 移除 DML 确认**

把 `frontend/src/renderer/src/components/SettingsDrawer.tsx` 的「安全参数」面板（约 line 528-535）替换为：

```tsx
                <div className="panel">
                  <div className="p-h">闸门参数<span className="p-s mono">运行时覆盖</span></div>
                  <div className="p-b">
                    <div className="set-row inline"><span className="sr-l">查询行数上限</span><input type="number" min="1" value={maxRows} onChange={(e) => setMaxRows(parseInt(e.target.value, 10) || 1000)} /></div>
                    <div className="set-row inline"><span className="sr-l">连接池大小</span><input type="number" min="1" value={poolSize} onChange={(e) => setPoolSize(parseInt(e.target.value, 10) || 3)} /><span className="hint" style={{ marginLeft: 6 }}>SQLite 固定 1 · PG/MySQL 默认 3</span></div>
                  </div>
                </div>
```

- [ ] **Step 5: 通用区只读值改动态**

把「通用」面板的三个只读 `input`（约 line 546-548）替换为：

```tsx
                    <div className="set-row readonly inline"><span className="sr-l">数据目录</span><input value={runtime?.data_dir ?? '~/.tabletalk'} readOnly /></div>
                    <div className="set-row readonly inline"><span className="sr-l">监听端口</span><input value={runtime?.port ?? 8765} readOnly /></div>
                    <div className="set-row readonly inline"><span className="sr-l">鉴权</span><input value={runtime?.auth ?? 'X-TableTalk-Token · 本机'} readOnly /></div>
```

- [ ] **Step 6: typecheck + build**

```bash
cd frontend && npm run typecheck && npm run build
```

预期：两者均通过。

- [ ] **Step 7: 提交（可选）**

```bash
cd frontend && git add src/renderer/src/components/SettingsDrawer.tsx && git commit -m "feat(settings-ui): bind query_max_rows/pool_size, dynamic runtime info, drop DML placeholder"
```

---

### Task 7: 全量回归

- [ ] **Step 1: 后端全量测试**

```bash
cd backend && .venv/bin/python -m pytest -q
```

预期：全部通过，无新增失败。

- [ ] **Step 2: 前端 typecheck + build**

```bash
cd frontend && npm run typecheck && npm run build
```

预期：通过。
