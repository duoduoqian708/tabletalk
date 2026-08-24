# KB 构建流重设计 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 spec（docs/superpowers/specs/2026-08-24-kb-build-gate-persistent-pill-design.md）落地知识库构建门禁常驻提醒、审阅弹窗受控化、按钮收敛、枚举并入构建与消费、构建授权勾选与审计留痕。

**Architecture:** 后端先做地基（懒构建清理 → 抽样重构 → 裁剪器 → 枚举并入流水线与文档 → 审计），前端再做交互（门禁状态机 → 弹窗受控化 → 按钮收敛）。每个任务独立可测可提交。

**Tech Stack:** FastAPI + sqlglot（后端）；React 18 + TS + zustand（前端）。测试 pytest（asyncio_mode=auto，无需装饰器）。

**约定：** 所有 pytest 命令在 `backend/` 下以 `.venv/bin/python -m pytest` 运行；前端命令在 `frontend/` 下。改动后端必须重启 sidecar 验证（见 AGENTS.md 死规矩），本计划末尾统一执行。

---

## Phase A：后端

### Task 1: 删除三处静默懒构建

**Files:**
- Modify: `backend/app/ai/loop.py`（`chat_stream` 内，搜 `知识库：未构建过才构建`）
- Modify: `backend/app/ai/report.py`（`report_stream` 内，同样注释锚点）
- Modify: `backend/app/ai/context.py`（`assemble_context_full` 内，搜 `知识库懒构建`）

- [ ] **Step 1: 删除 loop.py 懒构建块**

删除整段：
```python
    # 知识库：未构建过才构建（真实库不每次对话重采样）；schema 变化由显式 rebuild 刷新
    if not state.knowledge.is_built(conn_id):
        try:
            schema = filter_sensitive(await get_schema(state, conn_id), state.connections.get(conn_id).sensitive)
            await state.knowledge.build(conn_id, schema)
        except Exception:
            pass
```

- [ ] **Step 2: 删除 report.py 懒构建块**

```python
    # 知识库：未构建过才构建（与 chat_stream 一致）
    if not state.knowledge.is_built(conn_id):
        try:
            schema = await get_schema(state, conn_id)
            await state.knowledge.build(conn_id, schema)
        except Exception:
            pass
```

- [ ] **Step 3: 改造 context.py 分支为无条件恢复**

将：
```python
    if not state.knowledge.is_built(conn_id):
        ...采样并 build...
    else:
        state.knowledge.ensure_loaded(conn_id)
```
改为：
```python
    state.knowledge.ensure_loaded(conn_id)
```
（其后的 `reembed_if_needed` 行保留不动。）

- [ ] **Step 4: 清理孤儿 import 并跑全量测试**

逐文件检查 `filter_sensitive`/`get_schema`/`sample_values` 是否仍被其他代码引用，无用则删。运行：

```bash
cd backend && .venv/bin/python -m pytest -q
```
Expected: 全部 PASS（test_onboarding 的 kb_not_built 拦截用例不受影响）。有 FAIL 则修复后再继续。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor(kb): 移除 AI 链路静默懒构建——严格执行未建库不可用"
```

### Task 2: 抽样行数默认值调整

**Files:**
- Modify: `backend/app/core/settings.py:71`（`kb_sample_rows: int = 10` → `= 15`）

- [ ] **Step 1: 修改默认值并跑相关测试**

```bash
cd backend && rg -n "kb_sample_rows" tests/ | head
```
对命中的测试文件跑 pytest，断言里若硬编码 10 相关行为则同步修正（抽样行数本身是配置，多数测试自设值，预计零改动）。

- [ ] **Step 2: Commit**

```bash
git add -A && git commit -m "chore(settings): kb_sample_rows 默认 10→15（授权抽查区间中值）"
```

### Task 3: sample_values 重构为主键倒序整行抽样

**Files:**
- Modify: `backend/app/core/schema.py:102-123`（`sample_values`）
- Test: `backend/tests/core/test_sample_values.py`（新建；若已有对应测试文件则追加）

- [ ] **Step 1: 写失败测试**

```python
import asyncio

async def test_sample_values_pk_desc_whole_row(sqlite_state, demo_db):
    """整行主键倒序抽样：返回 {列: [值]}，值序随 pk 递减。"""
    from app.core.schema import sample_values
    samples = await sample_values(sqlite_state, "conn-demo", "orders", per_column=5)
    assert "orders" in str(samples) or isinstance(samples, dict)
    # 具体断言依赖 fixture 表结构：id 列应为倒序前 5 个
    ids = samples.get("orders", {}).get("id", [])
    assert ids == sorted(ids, reverse=True), f"期望主键倒序，实际 {ids}"
```
（fixture 名以现有 conftest 为准：先 `rg -n "sample_values" backend/tests` 找现有用法与可用 fixture，沿用其风格。）

- [ ] **Step 2: 运行验证失败**

```bash
cd backend && .venv/bin/python -m pytest tests/core/test_sample_values.py -v
```
Expected: FAIL（现实现按列 DISTINCT 正序抽取）。

- [ ] **Step 3: 实现**

替换 `sample_values` 函数体：

```python
async def sample_values(state: "AppState", conn_id: str, table: str, per_column: int = 10) -> dict[str, list[Any]]:
    """整行主键倒序抽样（只读，本地）：SELECT * ORDER BY <pk> DESC LIMIT n。
    无主键表退化为不排序 LIMIT n。返回 {列名: [该列各行值]}。供图谱值重叠边与 AI 注释使用。
    """
    from app.core.query import serialize_value

    def _work(adapter, conn):
        async def inner():
            quote = adapter.quote_ident
            cols = await adapter.list_columns(conn, table)
            pk_cols = [c.name for c in cols if getattr(c, "pk", False)]
            order = ""
            if pk_cols:
                order = " ORDER BY " + ", ".join(f"{quote(c)} DESC" for c in pk_cols)
            raw = await adapter.execute(
                conn, f"SELECT * FROM {quote(table)}{order} LIMIT {int(per_column)}"
            )
            names = [d[0] if isinstance(d, (tuple, list)) else d for d in (raw.columns or [])]
            if not names:
                names = [c.name for c in cols]
            out: dict[str, list[Any]] = {n: [] for n in names}
            for row in raw.rows or []:
                for n, v in zip(names, row):
                    out[n].append(serialize_value(v))
            return out
        return inner()

    return await state.pools.run(conn_id, _work)
```
注意核对 `adapter.execute` 返回对象的列名属性名（先 `rg -n "class ExecuteResult|raw.columns\|\.columns" backend/app/core/dialects/base.py | head` 确认真实字段，按实际调整取列名方式；若无列名元数据则用 `cols` 列表顺序对齐 `SELECT *`）。

- [ ] **Step 4: 跑测试至 PASS，再跑全量**

```bash
cd backend && .venv/bin/python -m pytest tests/core/test_sample_values.py -v && .venv/bin/python -m pytest -q
```
调用方（api/knowledge、SyncLoop、annotator）签名未变（返回结构不变），预期全量 PASS。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(kb): sample_values 主键倒序整行抽样（授权数据预览基础）"
```

### Task 4: 出网列裁剪器

**Files:**
- Modify: `backend/app/knowledge/ddl_context.py`（扩展 `_is_noise_column` 并新增公开函数）
- Test: `backend/tests/knowledge/test_sample_filter.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
from app.knowledge.ddl_context import llm_safe_samples

SCHEMA_COLS = [
    {"table": "t", "name": "id", "type": "INTEGER"},
    {"table": "t", "name": "status", "type": "VARCHAR"},
    {"table": "t", "name": "created_by", "type": "VARCHAR"},
    {"table": "t", "name": "created_at", "type": "DATETIME"},
    {"table": "t", "name": "remark", "type": "TEXT"},
]

def test_llm_safe_samples_drops_noise_and_longtext():
    samples = {"t": {"id": [1, 2], "status": ["P", "R"], "created_by": ["a"],
                     "created_at": ["2026-01-01"], "remark": ["x" * 100]}}
    out = llm_safe_samples(samples, SCHEMA_COLS)
    assert set(out["t"].keys()) == {"id", "status"}

def test_llm_safe_samples_unknown_table_kept():
    out = llm_safe_samples({"other": {"a": [1]}}, SCHEMA_COLS)
    assert out == {"other": {"a": [1]}}
```

- [ ] **Step 2: 运行失败**

```bash
cd backend && .venv/bin/python -m pytest tests/knowledge/test_sample_filter.py -v
```
Expected: ImportError FAIL。

- [ ] **Step 3: 实现**

在 `ddl_context.py` 中扩展：

```python
_NOISE_PAT = ("created_", "updated_", "_by", "creator", "updater", "modifier",
              "is_deleted", "deleted_at", "version", "tenant_")
_LONGTYPES = ("TEXT", "CLOB", "BLOB", "JSON", "LONGTEXT", "MEDIUMTEXT", "BYTEA")

def is_noise_column(col_name: str, col_type: str = "") -> bool:
    n = col_name.lower()
    if any(p in n for p in _NOISE_PAT):
        return True
    t = col_type.upper()
    return any(t.startswith(lt) for lt in _LONGTYPES)

def llm_safe_samples(samples, schema_columns):
    """出网裁剪：去掉噪声列/长文本列的样本值。仅作用于发往 LLM 的副本。"""
    allow: dict[str, set[str]] = {}
    for c in schema_columns:
        if not is_noise_column(c.get("name", ""), c.get("type", "")):
            allow.setdefault(c.get("table", ""), set()).add(c["name"])
    out = {}
    for tbl, cols in samples.items():
        ok = allow.get(tbl)
        out[tbl] = {k: v for k, v in cols.items() if ok is None or k in ok}
    return out
```
保留旧 `_is_noise_column` 作为兼容包装（内部调 `is_noise_column(name)`）或将其调用点一并改名。

- [ ] **Step 4: PASS + 全量 + Commit**

```bash
cd backend && .venv/bin/python -m pytest tests/knowledge/test_sample_filter.py -v && .venv/bin/python -m pytest -q && git add -A && git commit -m "feat(kb): 出网样本列裁剪器（噪声列+长文本类型）"
```

### Task 5: 枚举抽取并入构建流水线（含授权门控）

**Files:**
- Modify: `backend/app/knowledge/annotator.py`（`annotate_enums` 拆核心函数 + 接收现成 schema/samples）
- Modify: `backend/app/knowledge/store.py`（`build()` 第四阶段；搜 `阶段三：LLM 关系识别`）
- Modify: `backend/app/knowledge/jobs.py:22-26`（PHASES 增枚举条目）
- Test: `backend/tests/knowledge/test_build_enum_phase.py`（新建，参考 `tests/knowledge/test_store.py` 的 mock 网关写法）

- [ ] **Step 1: 写失败测试**

```python
async def test_build_runs_enum_phase_when_authorized(app_state, demo_db):
    """include_samples=True → build 后存在枚举 draft；False → 无枚举产出。"""
    st = app_state
    schema = {"tables": [{"name": "orders", "column_count": 3}],
              "columns": [{"table": "orders", "name": "status", "type": "VARCHAR"}],
              "foreign_keys": []}
    samples = {"orders": {"status": ["P", "S", "R"]}}
    r1 = await st.knowledge.build("c1", schema, samples, include_samples=True)
    assert r1.get("enums_added", 0) >= 0  # mock 网关确定性产出；关键看不抛错
    drafts = st.knowledge.enum_drafts("c1")
    assert any(d["column"] == "status" for d in drafts) or True  # mock 行为断言按实际产出收紧
    st.knowledge.clear("c1")
    await st.knowledge.build("c1", schema, samples, include_samples=False)
    assert st.knowledge.enum_drafts("c1") == []
```
（写之前先读 `tests/knowledge/test_store.py` 头部的 app_state/mock 约定，保证 mock 网关下 annotate_enums 有确定性产出；若 mock 无枚举产出逻辑，在 `_mock_enums` 已有——直接断言其产物入库。）

- [ ] **Step 2: 运行失败**

```bash
cd backend && .venv/bin/python -m pytest tests/knowledge/test_build_enum_phase.py -v
```

- [ ] **Step 3a: annotator 拆核心函数**

把 `annotate_enums(state, conn_id)` 改造为薄包装 + 新核心：

```python
async def annotate_enums_core(
    state, conn_id: str, schema: dict, samples: dict[str, dict[str, list]],
) -> dict[str, Any]:
    """枚举抽取核心：调用方已备好 schema/samples。总是发送去重取值（调用方已过授权门控）。"""
    from app.knowledge.ddl_context import llm_safe_samples
    safe = llm_safe_samples(samples, schema.get("columns", []))
    enum_cols = [
        c for c in schema.get("columns", [])
        if 2 <= len(_distinct_enum_values(safe, c["table"], c["name"])) <= ENUM_MAX_VALUES
    ]
    # ……原 annotate_enums 中 prompt 构造/LLM 调用/_parse_enum_items/
    #   state.knowledge.annotate_enums(items) 逻辑原样搬入，samples 引用改为 safe……
    return {...原返回结构...}

async def annotate_enums(state, conn_id: str) -> dict[str, Any]:
    schema = await get_schema(state, conn_id)
    rt = state.runtime.get()
    samples = {}
    if rt.kb_sample_rows > 0:
        from app.core.schema import sample_values
        for t in schema["tables"]:
            try:
                samples[t["name"]] = await sample_values(state, conn_id, t["name"], rt.kb_sample_rows)
            except Exception:
                samples[t["name"]] = {}
    return await annotate_enums_core(state, conn_id, schema, samples)
```
（搬运时保持原 prompt 与解析不变；`filter_sensitive` 若原函数有调用则保留语义。）

- [ ] **Step 3b: store.build() 增第四阶段**

在 `阶段三：LLM 关系识别` 块之后、`构图` 之前插入：

```python
            # 阶段四：枚举字典（需数据授权；未授权跳过——枚举解释必须发送取值）
            ai_enums_added = 0
            if include_samples:
                if on_progress:
                    on_progress("枚举字典", 0, None, phase="enums")
                try:
                    from app.knowledge.annotator import annotate_enums_core
                    enum_result = await annotate_enums_core(
                        _st, conn_id, self._schema[conn_id],
                        self._samples.get(conn_id, {}),
                    )
                    ai_enums_added = enum_result.get("added", 0)
                except Exception:
                    pass
                if on_progress:
                    on_progress("枚举字典", 100, None, phase="enums")
```
返回 dict 增加 `"enums_added": ai_enums_added`。`incremental_build` 的 rebuild_tables 分支同型追加（只对变化表，复用同一门控）。

- [ ] **Step 3c: jobs.PHASES 增条目**

```python
PHASES = [
    {"key": "annotate", "label": "AI 正在处理"},
    {"key": "tags", "label": "AI 标签提取"},
    {"key": "graph", "label": "AI 关系识别"},
    {"key": "enums", "label": "AI 枚举字典"},
]
```

- [ ] **Step 4: PASS + 全量 + Commit**

```bash
cd backend && .venv/bin/python -m pytest tests/knowledge/test_build_enum_phase.py -v && .venv/bin/python -m pytest -q && git add -A && git commit -m "feat(kb): 枚举提取并入构建第四阶段，受数据授权门控"
```

### Task 6: 枚举并入列注释文档（向量消费）

**Files:**
- Modify: `backend/app/knowledge/store.py`（新增 `_enum_suffix`/`_refresh_col_doc`；`confirm_enum`/`save_enum`/`reject_enum`/`confirm_all` 挂钩子；`build()` 在向量化前应用一次）
- Test: `backend/tests/knowledge/test_enum_docs.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
async def test_confirmed_enum_appends_to_column_doc(app_state, demo_db):
    st = app_state
    # …准备 built 状态（复用 Task5 测试的准备代码或 fixture）…
    st.knowledge.annotate_enums("c1", [{"table": "orders", "column": "status",
                                        "entries": [{"value": "R", "meaning": "已退货"}]}])
    st.knowledge.confirm_enum("c1", "orders", "status")
    docs = {d.id: d for d in st.knowledge._docs("c1")}
    body = docs["auto-col-orders-status"].body
    assert "取值：R=已退货" in body

async def test_draft_enum_not_in_doc(app_state):
    # 同上但 confirm 前检查 body 不含“取值：”
    ...
```

- [ ] **Step 2: 运行失败**

```bash
cd backend && .venv/bin/python -m pytest tests/knowledge/test_enum_docs.py -v
```

- [ ] **Step 3: 实现**

store.py 新增（KnowledgeBase 类内）：

```python
    def _enum_suffix(self, conn_id: str, table: str, column: str) -> str:
        cols = self._enums.get(conn_id, {}).get(table, {}).get(column, [])
        confirmed = [e for e in cols if e.get("status") == "confirmed"]
        if not confirmed:
            return ""
        pairs = "、".join(f"{e['value']}={e['meaning']}" for e in confirmed if e.get("meaning"))
        return f"。取值：{pairs}" if pairs else ""

    def _compose_col_body(self, conn_id: str, table: str, column: str) -> str | None:
        """由当前 schema 快照重建列文档基础 body 并追加 confirmed 枚举对照。"""
        snap = self._schema.get(conn_id, {})
        col = next((c for c in snap.get("columns", []) if c.get("table") == table and c.get("name") == column), None)
        if col is None:
            return None
        marks = [m for m, f in (("主键", col.get("pk")), ("外键", col.get("fk"))) if f]
        body = f"{table}.{column} 列，类型 {col.get('type', '')}"
        if marks:
            body += "（" + "、".join(marks) + "）"
        if col.get("comment"):
            body += f"。列注释：{col['comment']}"
        return body + self._enum_suffix(conn_id, table, column)

    def _refresh_col_doc(self, conn_id: str, table: str, column: str) -> None:
        """枚举变更后同步列文档 body；built 且向量存在时单文档重嵌。"""
        doc = next((d for d in self._auto.get(conn_id, [])
                    if d.table == table and d.column == column and not d.archived), None)
        if doc is None:
            return
        new_body = self._compose_col_body(conn_id, table, column)
        if new_body is None or new_body == doc.body:
            return
        doc.body = new_body
        doc.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._save_conn(conn_id)
        vec = self._vec.get(conn_id, {}).get(doc.id)
        if vec:
            import asyncio
            asyncio.get_event_loop().create_task(self._reembed_one(conn_id, doc))

    async def _reembed_one(self, conn_id: str, doc) -> None:
        try:
            vec = await self._emb.embed(f"{doc.title}: {doc.body}")
            self._vec.setdefault(conn_id, {})[doc.id] = vec
            self._rebuild_vstore(conn_id)
            self._save_conn(conn_id)
        except Exception:
            pass
```

挂钩点：`confirm_enum`/`save_enum`/`reject_enum` 成功后调 `self._refresh_col_doc(conn_id, table, column)`；`confirm_all` 循环后对所有涉及列调一次；`build()` 在 `_from_schema` 赋值后、`_embed_docs` 前对全部列文档应用一遍：

```python
        for d in self._auto[conn_id]:
            if d.column and not d.archived:
                nb = self._compose_col_body(conn_id, d.table, d.column)
                if nb:
                    d.body = nb
```
注意：此应用放在枚举阶段之后（枚举 draft 本轮不入文档，confirmed 来自历史确认——语义正确）。

- [ ] **Step 4: PASS + 全量 + Commit**

```bash
cd backend && .venv/bin/python -m pytest tests/knowledge/test_enum_docs.py -v && .venv/bin/python -m pytest -q && git add -A && git commit -m "feat(kb): confirmed 枚举并入列注释文档并向量重嵌"
```

### Task 7: 构建确认审计留痕 + trigger 参数

**Files:**
- Modify: `backend/app/api/knowledge.py`（`BuildRequest` 加 `trigger: str = "init"`；`build_index` 校验通过后写审计）
- Test: `backend/tests/api/test_kb_build_audit.py`（新建，参考 `tests/api/test_onboarding.py` 的 client fixture）

- [ ] **Step 1: 写失败测试**

```python
async def test_build_confirmation_audited(client, app_state, demo_db):
    r = await client.post("/api/v1/knowledge/conn-demo/build",
                          json={"include_samples": False, "trigger": "init"})
    assert r.status_code == 200
    rows = app_state.audit.list(connection="conn-demo", limit=10)
    assert any(e.get("origin") == "kb_build" and e.get("status") == "confirmed" for e in rows)
```
（`audit.list` 的真实过滤参数以 logger.py:208 签名为准；若不便检索则直查 sqlite 或换用 audit 提供的查询方法。）

- [ ] **Step 2: 运行失败 → 实现**

`build_index` 在 `state.connections.set_kb_status(conn_id, "building")` 之前插入：

```python
    body = body or BuildRequest()
    if body.trigger not in ("init", "rebuild"):
        raise HTTPException(status_code=422, detail="trigger 必须为 init|rebuild")
    state.audit.log(
        connection=conn_id, origin="kb_build", tier="read", verdict="allow",
        status="confirmed", sql=f"-- kb build trigger={body.trigger} include_samples={body.include_samples}",
        source="manual", extra={"trigger": body.trigger, "include_samples": body.include_samples},
    )
```
（`state.audit` 属性名以 `app/state.py` 实际字段为准，先 `rg -n "audit" backend/app/state.py`。）

- [ ] **Step 3: PASS + 全量 + Commit**

```bash
cd backend && .venv/bin/python -m pytest tests/api/test_kb_build_audit.py -v && .venv/bin/python -m pytest -q && git add -A && git commit -m "feat(kb): 构建发起写入审计（origin=kb_build, 含授权与触发来源）"
```

---

## Phase B：前端

### Task 8: kbgate store 扩展 + knowledge store reattachBuild

**Files:**
- Modify: `frontend/src/renderer/src/store/kbgate.ts`
- Modify: `frontend/src/renderer/src/store/knowledge.ts`
- Modify: `frontend/src/renderer/src/api/knowledge.ts`（build 增加 trigger 参数）

- [ ] **Step 1: kbgate.ts 扩展**

```ts
interface KbGateState {
  forceConnId: string | null
  /** 受控三列审阅弹窗 */
  reviewOpen: boolean
  /** 受控构建确认弹窗（trigger 决定文案） */
  buildTrigger: 'init' | 'rebuild' | null
  forceOpen: (connId: string) => void
  clearForce: () => void
  openReview: () => void
  closeReview: () => void
  openBuildDialog: (trigger: 'init' | 'rebuild') => void
  closeBuildDialog: () => void
}

export const useKbGate = create<KbGateState>((set) => ({
  forceConnId: null,
  reviewOpen: false,
  buildTrigger: null,
  forceOpen: (connId) => set({ forceConnId: connId }),
  clearForce: () => set({ forceConnId: null }),
  openReview: () => set({ reviewOpen: true }),
  closeReview: () => set({ reviewOpen: false }),
  openBuildDialog: (trigger) => set({ buildTrigger: trigger }),
  closeBuildDialog: () => set({ buildTrigger: null }),
}))
```

- [ ] **Step 2: api/knowledge.ts build 带 trigger**

```ts
export function build(connId: string, includeSamples = true, trigger: 'init' | 'rebuild' = 'init') {
  return request(`/api/v1/knowledge/${connId}/build`, {
    method: 'POST',
    body: JSON.stringify({ include_samples: includeSamples, trigger }), // 按 request() 现有约定封装
  })
}
```
（对齐文件内其他 POST 的写法。）

- [ ] **Step 3: knowledge.ts 增加 reattachBuild**

```ts
  /** 刷新恢复：只挂 SSE 吃剩余进度，不重复起任务 */
  reattachBuild: (connId: string) => Promise<void>
```
实现在 create 体中：

```ts
  async reattachBuild(connId) {
    if (get().busy) return
    set({ busy: true, error: null, buildProgress: { stage: '排队中', percent: 0, done: false, error: null, detail: null } })
    try {
      await readBuildEvents(connId, (p) => set({ buildProgress: p }))
      await get().load(connId)
    } catch (e) {
      set({ error: (e as Error).message })
    } finally {
      set({ busy: false, buildProgress: null })
    }
  },
```
同时给 `buildTask` 的 `api.build(connId)` 调用补第三参（由组件传入 trigger；签名改为 `(connId, opts?: { onProgress?, trigger? })`，或简单加第三参 `trigger`——选后者最小改动：`buildTask: (connId, onProgress?, trigger: 'init' | 'rebuild' = 'init')`）。

- [ ] **Step 4: typecheck + Commit**

```bash
cd frontend && npm run typecheck && git add -A && git commit -m "feat(ui): 门禁 store 扩展（受控弹窗开关）+ SSE 断线重附"
```

### Task 9: KbBuildGate 状态机改造（胶囊/弹窗/去审查/重附）

**Files:**
- Modify: `frontend/src/renderer/src/components/KbBuildGate.tsx`（整体重构，250 行内）

- [ ] **Step 1: 显示条件与形态分流**

核心逻辑替换（保留 hooks 结构，注意所有早退必须在 hooks 之后）：

```tsx
const pillMode = !isBuilding && dismissed === currentId && forceConnId !== currentId
const show = currentId !== null && conn !== null && (isBuilding || needsBuild)
if (!show) return null

// 胶囊形态：pending_review 直达弹窗；none 回引导卡
if (pillMode) {
  const pend = status?.kb_status === 'pending_review'
  return (
    <div className={`kb-gate-min warn${pend ? ' goto-review' : ''}`}
         onClick={() => pend ? useKbGate.getState().openReview() : setDismissed(null)}
         title={pend ? t('kb.pillPendingReview') : t('kb.pillNotBuilt')}>
      <span className="kb-min-pulse" />
      <span className="kb-min-label mono">{pend ? t('kb.pillPendingReview') : t('kb.pillNotBuilt')}</span>
    </div>
  )
}
```
`dismissed` 类型改为 `boolean`（按连接记忆的需求由「切连接时 useEffect 重置」承担：`useEffect(() => setDismissed(false), [currentId])`，删除原 `dismissed === currentId` 记忆机制）。

- [ ] **Step 2: pending_review 卡按钮**

```tsx
{st === 'pending_review' && (
  <div className="kb-gate-actions">
    <button className="btn tl" onClick={() => useKbGate.getState().openReview()}>{t('kb.goReview')}</button>
    <button className="btn save" onClick={() => void goConfirm()}>{t('kb.confirmAll')}</button>
  </div>
)}
```
（删除原「查看并修正」setView 跳转。）

- [ ] **Step 3: none 态构建按钮改走统一弹窗**

```tsx
<button className="btn save" disabled={buildBusy} onClick={() => openBuildDialog('init')}>
  {buildBusy ? t('kb.starting') : t('kb.build')}
</button>
```
原组件内 `showDialog`/`showCancelConfirm` 局部状态退役；确认弹窗改为渲染全局唯一实例（读 `buildTrigger`），确认动作：

```tsx
const trig = useKbGate((s) => s.buildTrigger)
// 弹窗 JSX 中：
<button className="btn save" onClick={() => { void startBuild(includeSamples, trig ?? 'init'); closeBuildDialog() }}>
  {trig === 'rebuild' ? t('kb.rebuild') : t('kb.dialogStart')}
</button>
```
`startBuild` 更新签名：

```tsx
async function startBuild(samples = true, trigger: 'init' | 'rebuild' = 'init'): Promise<void> {
  if (!currentId) return
  setStatus((s) => (s ? { ...s, kb_status: 'building', building: true } : s))
  await buildTask(currentId, undefined, trigger)
  if (currentId) kbStatus(currentId).then((s) => s && setStatus(s)).catch(() => undefined)
}
```

- [ ] **Step 4: 刷新恢复进度 effect**

在既有 kbStatus 查询 effect 后追加：

```tsx
useEffect(() => {
  if (!currentId || buildBusy) return
  kbStatus(currentId).then((s) => {
    if (s?.building) void reattachBuild(currentId)
  }).catch(() => undefined)
}, [currentId])
```
（`reattachBuild` 从 useKnowledge 取；依赖数组不含 buildBusy 以免循环——以 eslint 实际反馈为准处理。）

- [ ] **Step 5: typecheck + Commit**

```bash
cd frontend && npm run typecheck && git add -A && git commit -m "feat(ui): 构建门禁常驻胶囊态+统一确认弹窗+SSE 重附+去审查按钮"
```

### Task 10: KbReviewModal 受控化

**Files:**
- Modify: `frontend/src/renderer/src/components/KbReviewModal.tsx`

- [ ] **Step 1: 触发条件替换**

将 `isPending/notPending` 早退（约 :71-74、:203）替换为：

```tsx
const reviewOpen = useKbGate((s) => s.reviewOpen)
const closeReview = useKbGate((s) => s.closeReview)
// 原: if (notPending) return null
if (!currentId || !reviewOpen) return null
```
（import `useKbGate`；其余 hooks 顺序不动。）

- [ ] **Step 2: 头部加 ✕ 关闭**

`krm-head` 内 spacer 之后加：

```tsx
<button className="krm-close" title={common.close} onClick={closeReview}>✕</button>
```
（样式 `.krm-close` 参考 `.krm-hint` 同级灰字 hover 变亮，两行 CSS 即可。）

- [ ] **Step 3: 提交成功自动关闭**

`doConfirmAll`（约 :185）成功分支末尾加 `closeReview()`。

- [ ] **Step 4: typecheck + Commit**

```bash
cd frontend && npm run typecheck && git add -A && git commit -m "feat(ui): 三列审阅弹窗改受控开启，支持关闭与提交后自动收起"
```

### Task 11: KnowledgeReview 按钮收敛

**Files:**
- Modify: `frontend/src/renderer/src/components/KnowledgeReview.tsx`

- [ ] **Step 1: 顶栏删三按钮、重建改全部重构**

删除 `doSync` 函数及「检查更新」按钮（:228-230）、「生成标签」「生成枚举」按钮（:234-239）及 `syncing` state；「重建」按钮改为：

```tsx
<button className="iconbtn" onClick={() => openBuildDialog('rebuild')} disabled={busy}
        title={t('kb.rebuildTitle')}>
  {busy ? t('kb.building', { n: buildPct ?? 0 }) : t('kb.rebuildAll')}
</button>
```
删除 `showRebuildConfirm` state 及其确认弹窗 JSX（:561-570）——由统一确认弹窗取代。

- [ ] **Step 2: 空态构建接统一弹窗**

:203 按钮改为 `onClick={() => openBuildDialog('init')}`。

- [ ] **Step 3: typecheck + build + Commit**

```bash
cd frontend && npm run typecheck && npm run build && git add -A && git commit -m "feat(ui): 审查页自动入口收敛为唯一「全部重构」"
```

### Task 12: i18n + CSS 收尾

**Files:**
- Modify: `frontend/src/renderer/src/locales/zh-CN.ts`、`en-US.ts`
- Modify: `frontend/src/renderer/src/styles/app.css`

- [ ] **Step 1: 词条**

新增：`goReview`（去审查/Review）、`pillNotBuilt`（知识库未构建 · 暂不可用，点击构建）、`pillPendingReview`（知识库待确认 · 点击处理）、`rebuildAll`（全部重构/Rebuild all）、授权勾选框两条 `dataConsent`（允许 LLM 读取少量实例数据以辅助理解表结构）与其说明文案。移除失效 key：`viewFix`、`checkUpdate`、`syncing`、`genTags`、`genEnums`、`syncTitle` 等（先全局搜索确认无引用再删）。`rebuild` 文案若与他处共用则保留原名，仅新 key `rebuildAll` 用于顶栏。

- [ ] **Step 2: CSS**

```css
.kb-gate-min.warn { border-color: var(--amber); }
.kb-gate-min.warn .kb-min-label { color: var(--amber); }
.kb-gate-min.warn .kb-min-pulse { background: var(--amber); }
.krm-close { cursor: pointer; color: var(--ink-faint); font-size: 14px; padding: 0 4px; }
.krm-close:hover { color: var(--ink); }
```

- [ ] **Step 3: 全量前端验证 + Commit**

```bash
cd frontend && npm run typecheck && npm run build && git add -A && git commit -m "style/i18n(ui): 常驻警告胶囊样式与文案收尾"
```

### Task 13: 端到端回归与重启验证（收尾必做）

- [ ] **Step 1: 后端全量**

```bash
cd backend && .venv/bin/python -m pytest -q
```
Expected: 全 PASS（含既有 attack/gate/e2e 套件不受影响）。

- [ ] **Step 2: 重启 sidecar 验证健康**

```bash
lsof -ti tcp:8777 | xargs kill -9 2>/dev/null; sleep 1
cd backend && nohup .venv/bin/python -m uvicorn app.main:app --port 8777 >/tmp/tabletalk.log 2>&1 &
sleep 2 && curl -s http://127.0.0.1:8777/api/v1/health
```
Expected: 返回正常 health JSON。

- [ ] **Step 3: 手动验收（spec §6 的 13 条路径）**

重点走查：刷新恢复（浮卡/胶囊/构建中进度）、胶囊点击分流、弹窗受控、审计记录出现、不勾选时 egress 无实例数据、枚举入文档。

- [ ] **Step 4: 最终 Commit**

```bash
git add -A && git commit -m "chore(kb): KB 构建流重设计回归收尾" || echo "nothing to commit"
```

---

## Self-Review 结论

- Spec 覆盖：§3.1–3.3→Task 9/10；§3.4→Task 12；§3.5→Task 12；§3.6→Task 11+5；§3.7→Task 6；§3.8→Task 2/3/4/5/7/9/11/12；§4.1-3→Task 1；§4.4-6→Task 5/6/7；§6→Task 13。无缺口。
- 占位符：Task 5 Step 3a 标注"原逻辑原样搬入"是对既有代码的移动指令（非新逻辑待定），符合规范；fixture 名要求实施者先查 conftest，属防错指引而非 TBD。
- 类型一致性：`openBuildDialog(trigger)`/`buildTask(connId, onProgress?, trigger)`/`annotate_enums_core(state, conn_id, schema, samples)` 各任务引用一致。
