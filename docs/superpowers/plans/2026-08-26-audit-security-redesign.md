# 安全与审计页重构 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把安全与审计页重构为异常驱动主从操作台（左=三档统计+可搜索列表，右=今日时间线+详情），激活单机审批兜底入口，并把安全信号外提为状态栏徽章。

**Architecture:** 后端在现有 `AuditLogger`(SQLite audit.db) 与 `ApprovalStore`(system_db) 上增量扩展——新增 ack 表、signal/stats/游标分页端点、单机审批开放；前端重写 `AuditPage` 为 ListPane×DetailPane 组合并新增轮询 store。规格见 `docs/superpowers/specs/2026-08-26-audit-security-redesign-design.md`。

**Tech Stack:** FastAPI + SQLite(sqlite3 同步锁模式沿用) / React18 + TS + zustand + 手写 SVG（无图表库）。

---

## 现实校准（执行前必读，与 spec 的差异）

代码比 spec 写作时的假设新，以下为实地核实结论（2026-08-26）：

1. **审批后端已存在且测试完备**：`app/core/approvals.py`（ApprovalStore）+ `app/api/approvals.py`（create/list/approve/reject），approve 已实现重过闸门、只读拦截、rollback_ref、审计链（转审批/审批通过/审批执行完成）。**不重建，只增量改。**
2. **单机模式当前拒绝审批**（`POST /approvals` 返 400 "only in team mode"）。本计划开放单机（Task 6），这是审计页"待确认队列"能工作的前提。
3. **"当场批"沿用现有 confirm_token 通道**（query.py TOCTOU 防护，勿动）；spec §4.3 的"三合一走 approvals"改为：当场批=原通道直达，延迟批=转审批按钮（AiRail 已有）→ 审计页兜底。Task 13 会把该偏差补记进 spec。
4. **审批终态命名**：现实现为 `pending/approved/rejected`（approved 且有 `executed_audit_id` 即"已执行"）。不改枚举，避免破坏既有数据与测试。
5. `CLAUDE.md`/`AGENTS.md` 描述的"5 个 AI 工具/243 测试"等数字已过时，以代码为准。

---

### Task 1: AuditLogger — log() 返回行 id + audit_ack 未读体系

**Files:**
- Modify: `backend/app/audit/logger.py`
- Test: `backend/tests/api/test_audit_ack.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
"""audit_ack 未读体系 + log() 返回行 id。"""
from __future__ import annotations

from app.audit.logger import AuditLogger


def _log(lg: AuditLogger, verdict: str, conn: str = "demo"):
    return lg.log(connection=conn, origin="ai", tier="dml", verdict=verdict,
                  status="x", sql="UPDATE t SET a=1 WHERE id=1")


def test_log_returns_rowid(tmp_path):
    lg = AuditLogger(tmp_path)
    rid = _log(lg, "review")
    assert isinstance(rid, int) and rid > 0


def test_unread_count_and_ack(tmp_path):
    lg = AuditLogger(tmp_path)
    r1 = _log(lg, "block")
    _log(lg, "review")
    _log(lg, "allow")            # 放行不算异常
    assert lg.unread_exception_count() == 2
    lg.ack(r1)
    assert lg.unread_exception_count() == 1
    assert lg.ack_state(r1) == "ack"


def test_list_includes_ack_field(tmp_path):
    lg = AuditLogger(tmp_path)
    r1 = _log(lg, "block")
    entries = lg.list()
    assert entries[0]["ack"] == "unread"
    lg.ack(r1)
    assert lg.list()[0]["ack"] == "ack"


def test_unread_filter_by_connection(tmp_path):
    lg = AuditLogger(tmp_path)
    _log(lg, "block", conn="a")
    _log(lg, "block", conn="b")
    assert lg.unread_exception_count(connection="a") == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_audit_ack.py -q`
Expected: FAIL — `AttributeError: ... no attribute 'ack'`（或 log 返回 None 断言失败）

- [ ] **Step 3: 实现 logger.py**

3a. `_init_db` 中、`CREATE INDEX idx_audit_source` 之后追加：

```python
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS audit_ack (
                        audit_id INTEGER PRIMARY KEY REFERENCES audit_log(id),
                        state TEXT NOT NULL,
                        acked_ts TEXT
                    )
                    """
                )
```

3b. `log()` 末尾（`con.commit()` 之后、`finally` 之前）捕获返回值——把插入改为：

```python
                cur = con.execute(
                    """ ...(原 SQL 不变)... """,
                    ( ...原参数元组不变... ),
                )
                con.commit()
                return cur.lastrowid
```

并在方法签名后文档字符串无需变更。

3c. 文件末尾追加三个方法与共享行映射辅助：

```python
    def ack(self, audit_id: int) -> None:
        """标记某条审计为已处理（幂等）。"""
        with self._lock:
            con = sqlite3.connect(self.db_path)
            try:
                con.execute(
                    "INSERT OR REPLACE INTO audit_ack (audit_id, state, acked_ts) VALUES (?,?,?)",
                    (int(audit_id), "ack", time.strftime("%Y-%m-%dT%H:%M:%S")),
                )
                con.commit()
            finally:
                con.close()

    def ack_state(self, audit_id: int) -> str:
        with self._lock:
            con = sqlite3.connect(self.db_path)
            try:
                cur = con.execute("SELECT state FROM audit_ack WHERE audit_id=?", (int(audit_id),))
                row = cur.fetchone()
                return row[0] if row else "unread"
            finally:
                con.close()

    def unread_exception_count(self, connection: str | None = None) -> int:
        """未读异常数 = verdict∈{block,review} 且无 ack 记录。"""
        sql = (
            "SELECT COUNT(*) FROM audit_log a "
            "LEFT JOIN audit_ack k ON k.audit_id = a.id "
            "WHERE a.verdict IN ('block','review') AND k.audit_id IS NULL"
        )
        params: list[Any] = []
        if connection:
            sql += " AND a.connection = ?"
            params.append(connection)
        with self._lock:
            con = sqlite3.connect(self.db_path)
            try:
                return int(con.execute(sql, params).fetchone()[0])
            finally:
                con.close()
```

3d. `list()` 的查询改为带 join 并输出 ack 字段：
把 `sql = "SELECT * FROM audit_log"` 改为 `sql = "SELECT a.*, k.state AS ack_state FROM audit_log a LEFT JOIN audit_ack k ON k.audit_id = a.id"`（其余 where 拼接逻辑不动，注意后续引用列名的索引访问不受影响）；输出循环里 `out.append(e)` 之前加：

```python
                    e["ack"] = r["ack_state"] or "unread"
```

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_audit_ack.py tests/api/test_audit.py -q`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/audit/logger.py backend/tests/api/test_audit_ack.py
git commit -m "feat(audit): ack 未读体系 + log 返回行 id（信号外提数据基座）"
```

---

### Task 2: GET /api/v1/audit/signal 徽章端点

**Files:**
- Modify: `backend/app/api/audit.py`
- Test: `backend/tests/api/test_audit_signal.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
"""signal 端点：徽章计数。"""
from __future__ import annotations


async def test_signal_counts(app_state, client):
    app_state.audit.log(connection="demo", origin="ai", tier="dml",
                        verdict="block", status="拦截", sql="DELETE FROM t")
    app_state.audit.log(connection="demo", origin="ai", tier="dml",
                        verdict="review", status="需确认", sql="UPDATE t SET a=1 WHERE id=1")
    app_state.audit.log(connection="demo", origin="ai", tier="read",
                        verdict="allow", status="放行", sql="SELECT 1")
    app_state.approvals.create("conn-x", "UPDATE t SET a=1 WHERE id=1", "someone")
    r = await client.get("/api/v1/audit/signal")
    assert r.status_code == 200
    body = r.json()
    assert body["unread_exceptions"] == 2
    assert body["pending_approvals"] == 1
    assert body["today"]["blocked"] == 1 and body["today"]["review"] == 1


async def test_signal_connection_filter(app_state, client):
    app_state.audit.log(connection="a", origin="ai", tier="dml", verdict="block", status="拦截", sql="DELETE FROM t")
    r = await client.get("/api/v1/audit/signal", params={"connection": "b"})
    assert r.json()["unread_exceptions"] == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_audit_signal.py -q`
Expected: FAIL — 404 Not Found（路由不存在）

- [ ] **Step 3: 实现**

`backend/app/api/audit.py` 顶部 import 区加 `import time`，文件末尾追加：

```python
@router.get("/audit/signal")
async def audit_signal(connection: str | None = None) -> dict:
    """状态栏徽章轮询：未读异常 / 待审审批 / 今日拦截与待确认。极轻量。"""
    state = get_state()
    unread = state.audit.unread_exception_count(connection)
    pending = len(state.approvals.list(status="pending"))
    today_start = time.strftime("%Y-%m-%dT00:00:00")
    rows = state.audit.list(connection=connection, from_ts=today_start)
    blocked = sum(1 for e in rows if e.get("verdict") == "block")
    review = sum(1 for e in rows if e.get("verdict") == "review")
    return {
        "unread_exceptions": unread,
        "pending_approvals": pending,
        "today": {"blocked": blocked, "review": review},
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_audit_signal.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/audit.py backend/tests/api/test_audit_signal.py
git commit -m "feat(audit): GET /audit/signal 徽章轮询端点"
```

---

### Task 3: GET /api/v1/audit/stats 三档统计聚合

**Files:**
- Modify: `backend/app/audit/logger.py`、`backend/app/api/audit.py`
- Test: `backend/tests/api/test_audit_stats.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
"""stats 时间桶聚合（今日=小时桶 / 近N天=天桶）。"""
from __future__ import annotations

from app.audit.logger import AuditLogger


def _seed(lg: AuditLogger):
    lg.log(connection="c", origin="ai", tier="read", verdict="allow", status="s", sql="SELECT 1")
    lg.log(connection="c", origin="ai", tier="dml", verdict="block", status="s", sql="DELETE FROM t")
    lg.log(connection="c", origin="ai", tier="dml", verdict="review", status="s", sql="UPDATE t SET a=1 WHERE id=1")


def test_hour_buckets_today(tmp_path):
    import time
    lg = AuditLogger(tmp_path); _seed(lg)
    today_start = time.strftime("%Y-%m-%dT00:00:00")
    buckets = lg.stats_buckets(connection=None, since_ts=today_start, fmt="%Y-%m-%dT%H:00:00")
    total = sum(b["total"] for b in buckets)
    assert total == 3
    assert sum(b["block"] for b in buckets) == 1
    assert sum(b["review"] for b in buckets) == 1
    assert all(len(b["bucket"]) == 13 for b in buckets)  # YYYY-MM-DDTHH


def test_day_buckets_range(tmp_path):
    lg = AuditLogger(tmp_path); _seed(lg)
    buckets = lg.stats_buckets(connection=None, since_ts="2000-01-01T00:00:00", fmt="%Y-%m-%dT00:00:00")
    assert len(buckets) >= 1
    assert all(bucket.endswith("T00:00:00") for bucket in [b["bucket"] for b in buckets])


def test_connection_scoped(tmp_path):
    lg = AuditLogger(tmp_path); _seed(lg)
    lg.log(connection="other", origin="ai", tier="dml", verdict="block", status="s", sql="DELETE FROM x")
    buckets = lg.stats_buckets(connection="other", since_ts="2000-01-01T00:00:00", fmt="%Y-%m-%dT00:00:00")
    assert sum(b["total"] for b in buckets) == 1


async def test_endpoint_scopes(client):
    r = await client.get("/api/v1/audit/stats", params={"scope": "7d"})
    assert r.status_code == 200
    body = r.json()
    assert "buckets" in body and body["granularity"] == "day"
    r2 = await client.get("/api/v1/audit/stats", params={"scope": "today"})
    assert r2.json()["granularity"] == "hour"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_audit_stats.py -q`
Expected: FAIL — no attribute `stats_buckets` / 端点 404

- [ ] **Step 3: 实现**

3a. `logger.py` 追加方法：

```python
    def stats_buckets(self, connection: str | None, since_ts: str, fmt: str) -> list[dict[str, Any]]:
        """时间桶聚合：fmt 为 SQLite strftime 格式（仅内部常量调用，无注入面）。"""
        sql = (
            "SELECT strftime('" + fmt + "', ts) AS bucket, COUNT(*) AS total, "
            "SUM(CASE WHEN verdict='allow' THEN 1 ELSE 0 END) AS allow, "
            "SUM(CASE WHEN verdict='review' THEN 1 ELSE 0 END) AS review, "
            "SUM(CASE WHEN verdict='block' THEN 1 ELSE 0 END) AS block "
            "FROM audit_log WHERE ts >= ?"
        )
        params: list[Any] = [since_ts]
        if connection:
            sql += " AND connection = ?"
            params.append(connection)
        sql += " GROUP BY bucket ORDER BY bucket"
        with self._lock:
            con = sqlite3.connect(self.db_path)
            con.row_factory = sqlite3.Row
            try:
                return [
                    {"bucket": r["bucket"], "total": int(r["total"]),
                     "allow": int(r["allow"] or 0), "review": int(r["review"] or 0),
                     "block": int(r["block"] or 0)}
                    for r in con.execute(sql, params).fetchall()
                ]
            finally:
                con.close()
```

3b. `api/audit.py` 追加端点（import 区加 `import datetime as _dt`）：

```python
@router.get("/audit/stats")
async def audit_stats(scope: str = "30d", connection: str | None = None) -> dict:
    """三档统计：today=小时桶(当日0点起)；7d/30d=天桶。时区=本地。"""
    now = _dt.datetime.now()
    if scope == "today":
        since = now.strftime("%Y-%m-%dT00:00:00"); fmt = "%Y-%m-%dT%H:00:00"; gran = "hour"
    elif scope == "7d":
        since = (now - _dt.timedelta(days=6)).strftime("%Y-%m-%dT00:00:00"); fmt = "%Y-%m-%dT00:00:00"; gran = "day"
    else:
        since = (now - _dt.timedelta(days=29)).strftime("%Y-%m-%dT00:00:00"); fmt = "%Y-%m-%dT00:00:00"; gran = "day"
    buckets = get_state().audit.stats_buckets(connection, since, fmt)
    return {"scope": scope, "granularity": gran, "buckets": buckets}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_audit_stats.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/audit/logger.py backend/app/api/audit.py backend/tests/api/test_audit_stats.py
git commit -m "feat(audit): stats 时间桶聚合（today/7d/30d 三档）"
```

---

### Task 4: /audit 游标分页 + q 搜索 + 异常过滤

**Files:**
- Modify: `backend/app/audit/logger.py`、`backend/app/api/audit.py`
- Test: `backend/tests/api/test_audit_page.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
"""keyset 分页 + LIKE 搜索 + 异常/未读过滤（page()）。"""
from __future__ import annotations

from app.audit.logger import AuditLogger


def _seed(lg: AuditLogger):
    for i in range(30):
        v = ["allow", "review", "block"][i % 3]
        lg.log(connection="c", origin="ai", tier="dml", verdict=v, status="s",
               sql=f"UPDATE t{i} SET a=1 WHERE id={i}")


def test_page_desc_and_cursor(tmp_path):
    lg = AuditLogger(tmp_path); _seed(lg)
    p1 = lg.page(limit=10)
    assert len(p1["items"]) == 10 and p1["has_more"] is True
    ids1 = [e["_id"] for e in p1["items"]]
    assert ids1 == sorted(ids1, reverse=True)
    p2 = lg.page(limit=10, before_id=ids1[-1])
    ids2 = [e["_id"] for e in p2["items"]]
    assert max(ids2) < min(ids1)
    assert p2["has_more"] is True


def test_page_q_search(tmp_path):
    lg = AuditLogger(tmp_path); _seed(lg)
    p = lg.page(q="t7 ", limit=100)
    assert len(p["items"]) == 1 and "t7" in p["items"][0]["sql"]
    p_pct = lg.page(q="%", limit=100)   # 通配符须被转义
    assert p_pct["items"] == []


def test_page_exception_unread(tmp_path):
    lg = AuditLogger(tmp_path); _seed(lg)
    p = lg.page(exception=True, limit=100)
    assert len(p["items"]) == 20                       # review+block
    assert all(e["verdict"] in ("block", "review") for e in p["items"])
    lg.ack(p["items"][0]["_id"])
    p2 = lg.page(exception=True, unread_only=True, limit=100)
    assert len(p2["items"]) == 19


async def test_endpoint_cursor_mode(client, app_state):
    app_state.audit.log(connection="c", origin="ai", tier="dml", verdict="block", status="s", sql="DELETE FROM t")
    r = await client.get("/api/v1/audit", params={"exception": "true", "cursor": "0", "limit": 10})
    body = r.json()
    assert "items" in body and "next_cursor" in body
    assert body["items"][0]["verdict"] == "block"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_audit_page.py -q`
Expected: FAIL — no attribute `page`

- [ ] **Step 3: 实现**

3a. `logger.py`：把 `list()` 内"行→dict"的整段循环体抽成模块级函数 `_row_to_entry(r)`（原样搬运，另加两行）：

```python
def _row_to_entry(r: sqlite3.Row) -> dict[str, Any]:
    e: dict[str, Any] = {
        "_id": r["id"],
        "schema_version": r["schema_version"],
        # ...以下与原 list() 循环体逐字段一致（ts/connection/origin/tier/verdict/status/sql/
        # elapsed_ms/report_id/source/reasons/tables/manifest/approval_id/rollback_ref/
        # estimated_rows/cost_degraded/extra_json 合并/ack 字段）...
    }
    return e
```

然后 `list()` 收尾改为 `out.append(_row_to_entry(r))`；`page()` 复用同一映射。

3b. `logger.py` 追加 `page()`：

```python
    def page(
        self,
        *,
        connection: str | None = None,
        origin: str | None = None,
        tier: str | None = None,
        verdict: str | None = None,
        q: str | None = None,
        exception: bool = False,
        unread_only: bool = False,
        from_ts: str | None = None,
        to_ts: str | None = None,
        before_id: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """keyset 分页（id 倒序）。返回 {items, has_more}；items 含 _id 供游标续传。"""
        where: list[str] = []
        params: list[Any] = []
        if connection:
            where.append("a.connection = ?"); params.append(connection)
        if origin:
            where.append("a.origin = ?"); params.append(origin)
        if tier:
            where.append("a.tier = ?"); params.append(tier)
        if verdict:
            where.append("a.verdict = ?"); params.append(verdict)
        if exception:
            where.append("a.verdict IN ('block','review')")
        if from_ts:
            where.append("a.ts >= ?"); params.append(from_ts)
        if to_ts:
            where.append("a.ts <= ?"); params.append(to_ts)
        if q:
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("a.sql LIKE ? ESCAPE '\\'"); params.append(f"%{esc}%")
        if unread_only:
            where.append("NOT EXISTS (SELECT 1 FROM audit_ack k WHERE k.audit_id = a.id)")
        if before_id is not None:
            where.append("a.id < ?"); params.append(int(before_id))
        sql = (
            "SELECT a.*, k.state AS ack_state FROM audit_log a "
            "LEFT JOIN audit_ack k ON k.audit_id = a.id"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY a.id DESC LIMIT ?"
        params.append(int(limit) + 1)
        with self._lock:
            con = sqlite3.connect(self.db_path)
            con.row_factory = sqlite3.Row
            try:
                rows = con.execute(sql, params).fetchall()
                has_more = len(rows) > limit
                items = [_row_to_entry(r) for r in rows[:limit]]
                return {"items": items, "has_more": has_more}
            finally:
                con.close()
```

3c. `api/audit.py` 的 `/audit` 路由签名加参数并在命中新模式参数时切换响应形态：

```python
@router.get("/audit")
async def audit_log(
    connection: str | None = None,
    origin: str | None = None,
    tier: str | None = None,
    verdict: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    from_ts: str | None = None,
    to_ts: str | None = None,
    report_id: str | None = None,
    source: str | None = None,
    q: str | None = None,
    exception: bool = False,
    unread_only: bool = False,
    cursor: int | None = None,
) -> dict:
    state = get_state()
    cursor_mode = cursor is not None or bool(q) or exception or unread_only
    if cursor_mode:
        res = state.audit.page(
            connection=connection, origin=origin, tier=tier, verdict=verdict,
            q=q, exception=exception, unread_only=unread_only,
            from_ts=from_ts, to_ts=to_ts,
            before_id=int(cursor) if cursor and cursor > 0 else None,
            limit=min(limit, 200),
        )
        next_cursor = res["items"][-1]["_id"] if res["has_more"] and res["items"] else None
        return {"items": res["items"], "next_cursor": next_cursor}
    full = state.audit.list(
        connection=connection, origin=origin, tier=tier, verdict=verdict,
        from_ts=from_ts, to_ts=to_ts, report_id=report_id, source=source,
    )
    total = len(full)
    window = full[::-1][offset:offset + limit]
    return {"count": total, "entries": window}
```

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_audit_page.py tests/api/test_audit.py tests/api/test_kb_build_audit.py -q`
Expected: PASS（旧形态 {count,entries} 保持兼容）

- [ ] **Step 5: Commit**

```bash
git add backend/app/audit/logger.py backend/app/api/audit.py backend/tests/api/test_audit_page.py
git commit -m "feat(audit): keyset 分页 + 服务端 LIKE 搜索 + 异常/未读过滤"
```

---

### Task 5: 出网/周报补时间范围

**Files:**
- Modify: `backend/app/api/audit.py`（egress/weekly 两端点）
- Test: `backend/tests/api/test_audit_page.py` 追加

- [ ] **Step 1: 追加失败测试**（同文件末尾）

```python
async def test_egress_weekly_time_range(client, app_state):
    app_state.audit.log(connection="c", origin="ai", tier="read", verdict="egress",
                        status="出网", sql="-", manifest={"model": "m1"})
    future = "2099-01-01T00:00:00"
    r1 = await client.get("/api/v1/audit/egress", params={"to_ts": "2000-01-01T00:00:00"})
    assert r1.json()["total"] == 0
    r2 = await client.get("/api/v1/audit/egress", params={"to_ts": future})
    assert r2.json()["total"] == 1
    r3 = await client.get("/api/v1/audit/weekly", params={"to_ts": "2000-01-01T00:00:00"})
    assert r3.json()["total"] == 0
```

- [ ] **Step 2: 跑确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_audit_page.py::test_egress_weekly_time_range -q`
Expected: FAIL — weekly 忽略 to_ts 导致 total!=0

- [ ] **Step 3: 实现**：两个端点签名各加 `from_ts: str | None = None, to_ts: str | None = None`，并把参数透传到内部 `state.audit.list(...)` 调用（weekly 目前无任何过滤参数，全部加上）。

- [ ] **Step 4: 跑确认通过**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_audit_page.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/audit.py backend/tests/api/test_audit_page.py
git commit -m "fix(audit): egress/weekly 支持 from_ts/to_ts 时间范围"
```

---

### Task 6: 单机开放审批 + Approval 扩展字段 + 执行回填

**Files:**
- Modify: `backend/app/core/approvals.py`、`backend/app/api/approvals.py`
- Modify: `backend/tests/api/test_approvals.py`（翻转单机用例）
- Test: `backend/tests/api/test_approvals_single.py`（新建）

- [ ] **Step 1: 更新旧测试 + 写新失败测试**

`tests/api/test_approvals.py` 中删除 `test_single_mode_rejected`，替换为：

```python
async def test_single_mode_create_allowed(app_state, conn_id, client, monkeypatch):
    monkeypatch.delenv("TABLETALK_AUTH_MODE", raising=False)
    r = await client.post(
        "/api/v1/approvals", json={"connection_id": conn_id, "sql": _WITH_WHERE}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
```

新建 `tests/api/test_approvals_single.py`：

```python
"""单机模式全链路：创建→批准→执行→executed_audit_id/rollback_ref 回填。"""
from __future__ import annotations

_WITH_WHERE = "UPDATE products SET price = 9 WHERE id = 1"


async def test_single_mode_full_chain(app_state, conn_id, demo_db, client, monkeypatch):
    monkeypatch.delenv("TABLETALK_AUTH_MODE", raising=False)
    r = await client.post("/api/v1/approvals", json={"connection_id": conn_id, "sql": _WITH_WHERE})
    aid = r.json()["id"]

    # 创建即快照影响行数预览
    created = app_state.approvals.get(aid)
    assert created.preview_rows is not None and created.preview_rows >= 1

    r2 = await client.post(f"/api/v1/approvals/{aid}/approve", json={})
    assert r2.status_code == 200, r2.text
    a = app_state.approvals.get(aid)
    assert a.status == "approved"
    assert isinstance(a.executed_audit_id, int) and a.executed_audit_id > 0
    assert a.rollback_ref  # 有回滚剧本哈希


async def test_single_mode_reject_allowed(app_state, conn_id, client, monkeypatch):
    monkeypatch.delenv("TABLETALK_AUTH_MODE", raising=False)
    r = await client.post("/api/v1/approvals", json={"connection_id": conn_id, "sql": _WITH_WHERE})
    aid = r.json()["id"]
    r2 = await client.post(f"/api/v1/approvals/{aid}/reject", json={"note": "先不动"})
    assert r2.status_code == 200 and r2.json()["status"] == "rejected"


async def test_double_decide_conflict(app_state, conn_id, client, monkeypatch):
    monkeypatch.delenv("TABLETALK_AUTH_MODE", raising=False)
    r = await client.post("/api/v1/approvals", json={"connection_id": conn_id, "sql": _WITH_WHERE})
    aid = r.json()["id"]
    await client.post(f"/api/v1/approvals/{aid}/reject", json={})
    r2 = await client.post(f"/api/v1/approvals/{aid}/approve", json={})
    assert r2.status_code in (404, 409)   # 非 pending 一律拒绝（幂等底线）
```

同时把 `tests/api/test_approvals.py::test_non_admin_approve_forbidden` 保留不动（team 模式角色语义不变）。

- [ ] **Step 2: 跑确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_approvals_single.py tests/api/test_approvals.py -q`
Expected: FAIL — 单机仍 400 / Approval 无 preview_rows 字段

- [ ] **Step 3: 实现**

3a. `core/approvals.py` 的 `Approval` dataclass 追加三个带默认值字段（旧 JSON 数据加载兼容）：

```python
    preview_rows: int | None = None
    executed_audit_id: int | None = None
    rollback_ref: str | None = None
```

`ApprovalStore` 追加：

```python
    def attach_execution(self, aid: str, *, executed_audit_id: int, rollback_ref: str | None = None) -> Approval | None:
        a = self._items.get(aid)
        if not a:
            return None
        a.executed_audit_id = executed_audit_id
        if rollback_ref:
            a.rollback_ref = rollback_ref
        self._save()
        return a

    def set_preview(self, aid: str, preview_rows: int | None) -> None:
        a = self._items.get(aid)
        if a:
            a.preview_rows = preview_rows
            self._save()
```

3b. `api/approvals.py`：

- `create_approval`：删除开头的 team-mode 400 块；创建成功后快照预览：

```python
    a = state.approvals.create(req.connection_id, req.sql, uid)
    if tier == "dml":
        try:
            from app.safety import gate as _g2
            prev = await _g2.preview_rows(state, req.connection_id, req.sql, _gate.sqlglot_dialect_for(cfg.dialect))
            n = (prev or {}).get("count")
            state.approvals.set_preview(a.id, int(n) if n is not None else None)
        except Exception:
            pass
```

（`preview_rows()` 返回结构以 `app/safety/gate.py` 实测为准——若键名非 `count` 则按实际取行数键；实现时先跑一行 REPL 确认再定。）

- `list_approvals`：删除 team-mode 400 块。
- `approve` 与 `reject` 的角色门禁替换为：

```python
    if state.auth.is_team_mode():
        user = getattr(request.state, "user", None)
        role = user.get("role") if isinstance(user, dict) else None
        if role != "admin":
            raise HTTPException(status_code=403, detail="admin required")
```

（原独立 `role != "admin"` 判断删除。）

- `approve` 执行成功分支：`state.audit.log(... status="审批执行完成" ...)` 现在会返回 rowid（Task 1），接住并回填：

```python
        exec_audit_id = state.audit.log(...)   # 原"审批执行完成"那条
        try:
            state.approvals.attach_execution(a.id, executed_audit_id=int(exec_audit_id), rollback_ref=rollback_ref)
        except Exception:
            pass
```

- 幂等：store.approve/reject 非 pending 已返回 None→404，满足 `test_double_decide_conflict`，无需额外改动。

- [ ] **Step 4: 跑确认通过 + 审批回归**

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_approvals_single.py tests/api/test_approvals.py tests/api/test_audit_signal.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/approvals.py backend/app/api/approvals.py backend/tests/api/test_approvals.py backend/tests/api/test_approvals_single.py
git commit -m "feat(approvals): 单机模式开放审批流 + 预览/执行审计回填字段"
```

---

### Task 7: 后端收尾验证 + 重启 sidecar（仓库死规矩）

- [ ] **Step 1: 全量后端测试**

Run: `cd backend && .venv/bin/python -m pytest -q`
Expected: 全绿（新增 ~15 个用例 + 存量全过）

- [ ] **Step 2: 重启 sidecar（改完后端必做）**

```bash
lsof -ti tcp:8777 | xargs kill -9 2>/dev/null; sleep 1
cd backend && nohup .venv/bin/python -m uvicorn app.main:app --port 8777 >/tmp/tabletalk-uvicorn.log 2>&1 &
sleep 2 && curl -s http://127.0.0.1:8777/api/v1/health
```

Expected: `{"status":"ok",...}`（若用户另有启动方式则遵从之）

---

### Task 8: 前端 API 层 — approvals.ts + audit.ts 扩展

**Files:**
- Create: `frontend/src/renderer/src/api/approvals.ts`
- Modify: `frontend/src/renderer/src/api/audit.ts`、`frontend/src/renderer/src/api/types.ts`

- [ ] **Step 1: 新建 `api/approvals.ts`**

```typescript
import { request } from './client'

export interface ApprovalItem {
  id: string
  connection_id: string
  sql: string
  requested_by: string
  requested_at: string
  status: 'pending' | 'approved' | 'rejected'
  reviewed_by?: string | null
  reviewed_at?: string | null
  note?: string | null
  preview_rows?: number | null
  executed_audit_id?: number | null
  rollback_ref?: string | null
}

export async function listApprovals(status?: string): Promise<{ items: ApprovalItem[] }> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : ''
  return request(`/api/v1/approvals${qs}`)
}

export async function approveApproval(id: string): Promise<{ id: string; result?: unknown }> {
  return request(`/api/v1/approvals/${id}/approve`, { method: 'POST', body: JSON.stringify({}) })
}

export async function rejectApproval(id: string, note?: string): Promise<{ id: string; status: string }> {
  return request(`/api/v1/approvals/${id}/reject`, { method: 'POST', body: JSON.stringify({ note }) })
}
```

（核对 `client.ts::request` 的实际签名——若 body 需包 `headers` 由 request 内部统一注入 Content-Type，则以现状为准微调，保持与其他 api/*.ts 完全同构。）

- [ ] **Step 2: 扩展 `api/audit.ts`**

追加类型与函数（保留现有导出不动）：

```typescript
export interface SignalInfo { unread_exceptions: number; pending_approvals: number; today: { blocked: number; review: number } }
export interface StatBucket { bucket: string; total: number; allow: number; review: number; block: number }

export async function auditSignal(): Promise<SignalInfo> {
  return request('/api/v1/audit/signal')
}

export async function auditStats(scope: 'today' | '7d' | '30d', connection?: string): Promise<{ scope: string; granularity: string; buckets: StatBucket[] }> {
  const p = new URLSearchParams({ scope })
  if (connection) p.set('connection', connection)
  return request(`/api/v1/audit/stats?${p}`)
}

export async function ackAudit(id: number): Promise<void> {
  await request(`/api/v1/audit/${id}/ack`, { method: 'POST', body: '{}' })
}

export interface PageQuery { connection?: string; q?: string; exception?: boolean; unread_only?: boolean; verdict?: string; origin?: string; tier?: string; from_ts?: string; cursor?: number; limit?: number }

export async function listAuditPage(query: PageQuery): Promise<{ items: (AuditEntry & { _id: number })[]; next_cursor: number | null }> {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== '' && v !== false) p.set(k, String(v === true ? 'true' : v))
  }
  return request(`/api/v1/audit?${p}`)
}
```

- [ ] **Step 3: `types.ts` 的 `AuditEntry` 增加可选字段**

```typescript
  ack?: 'unread' | 'ack'
  manifest?: Record<string, unknown>
```

- [ ] **Step 4: 实现 ack 端点**（前端依赖它，回到后端补一个 3 行路由——放这里是因为属于前端任务的最小闭环）

`backend/app/api/audit.py`：

```python
class AckRequest(BaseModel):
    state: str = "ack"


@router.post("/audit/{audit_id}/ack")
async def ack_audit(audit_id: int, req: AckRequest) -> dict:
    get_state().audit.ack(int(audit_id))
    return {"ok": True}
```

（import 区补 `from pydantic import BaseModel`。）对应后端小测试追加到 `tests/api/test_audit_ack.py`：

```python
async def test_ack_endpoint(client, tmp_path):
    r = await client.post("/api/v1/audit/1/ack", json={})
    assert r.status_code == 200
```

Run: `cd backend && .venv/bin/python -m pytest tests/api/test_audit_ack.py -q && cd ../frontend && npm run typecheck`
Expected: 后端 PASS；typecheck 无错误

- [ ] **Step 5: Commit**

```bash
git add frontend/src/renderer/src/api backend/app/api/audit.py backend/tests/api/test_audit_ack.py
git commit -m "feat(web): 审计/审批 API 客户端层（signal/stats/page/ack）"
```

---

### Task 9: auditSignal store + 状态栏徽章

**Files:**
- Create: `frontend/src/renderer/src/store/auditSignal.ts`
- Create: `frontend/src/renderer/src/components/AuditBadge.tsx`
- Modify: `frontend/src/renderer/src/components/AppLayout.tsx`（footer 挂载 + 启停）
- Modify: `frontend/src/renderer/src/styles/deck.css`（徽章样式）

- [ ] **Step 1: 新建 `store/auditSignal.ts`**

```typescript
import { create } from 'zustand'
import { auditSignal } from '@renderer/api/audit'

/** 轮询间隔：用户要求易于调整（可能改 10s），改这一个常量即可 */
export const SIGNAL_POLL_INTERVAL = 30_000

interface SignalState {
  unread: number
  pending: number
  todayBlocked: number
  todayReview: number
  refresh: () => Promise<void>
  start: () => void
  stop: () => void
}

let timer: number | undefined

export const useAuditSignal = create<SignalState>((set) => ({
  unread: 0,
  pending: 0,
  todayBlocked: 0,
  todayReview: 0,
  refresh: async () => {
    try {
      const s = await auditSignal()
      set({ unread: s.unread_exceptions, pending: s.pending_approvals, todayBlocked: s.today.blocked, todayReview: s.today.review })
    } catch { /* 静默降级：徽章保持上次值 */ }
  },
  start: () => {
    if (timer !== undefined) return
    void useAuditSignal.getState().refresh()
    timer = window.setInterval(() => void useAuditSignal.getState().refresh(), SIGNAL_POLL_INTERVAL)
  },
  stop: () => {
    if (timer !== undefined) { window.clearInterval(timer); timer = undefined }
  },
}))
```

- [ ] **Step 2: 新建 `components/AuditBadge.tsx`**

```tsx
import { useAuditSignal } from '@renderer/store/auditSignal'
import { useUi } from '@renderer/store/ui'
import { useI18n } from '@renderer/store/i18n'

/** 状态栏安全徽章：无事显示平安，有事显示可点击计数（跳审计页）。 */
export function AuditBadge(): React.JSX.Element {
  const { t } = useI18n()
  const unread = useAuditSignal((s) => s.unread)
  const pending = useAuditSignal((s) => s.pending)
  const setView = useUi((s) => s.setView)
  if (!unread && !pending) {
    return <span className="sig-pill ok">✓ {t('audit.allClear')}</span>
  }
  return (
    <span className="sig-group">
      {unread > 0 && (
        <button className="sig-pill danger" onClick={() => setView('audit')}>
          ● {t('audit.unreadN', { n: unread })}
        </button>
      )}
      {pending > 0 && (
        <button className="sig-pill warn" onClick={() => setView('audit')}>
          ◔ {t('audit.pendingN', { n: pending })}
        </button>
      )}
    </span>
  )
}
```

- [ ] **Step 3: AppLayout 挂载**

`AppLayout.tsx` footer `<footer className="statusline deck-status">` 内 `<span className="mid">` 之后加：

```tsx
          <AuditBadge />
```

import 区加：

```tsx
import { AuditBadge } from './AuditBadge'
import { useAuditSignal } from '@renderer/store/auditSignal'
```

组件体内加启停 effect（放在现有 useEffect 群尾部）：

```tsx
  // 安全徽章轮询启停（随应用生命周期）
  useEffect(() => {
    useAuditSignal.getState().start()
    return () => useAuditSignal.getState().stop()
  }, [])
```

- [ ] **Step 4: deck.css 末尾追加样式（只用 tokens 变量）**

```css
/* ── 状态栏安全徽章 ── */
.sig-group { display: inline-flex; gap: 8px; margin-left: 12px; }
.sig-pill {
  display: inline-flex; align-items: center; gap: 4px;
  border-radius: 999px; padding: 1px 10px; font-size: 12px;
  border: 1px solid var(--line-strong); color: var(--ink-dim); background: transparent;
}
.sig-pill.ok { color: var(--green); border-color: var(--green-dim); background: var(--green-dim); }
.sig-pill.danger { color: var(--red); border-color: var(--red-dim); background: var(--red-dim); cursor: pointer; }
.sig-pill.warn { color: var(--amber); border-color: var(--amber-dim); background: var(--amber-dim); cursor: pointer; }
```

- [ ] **Step 5: 验证**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: 双绿。手动：浏览器打开 `http://127.0.0.1:8777`，底部应出现"✓ 今日平安"（或真实计数）。

- [ ] **Step 6: Commit**

```bash
git add frontend/src/renderer/src/store/auditSignal.ts frontend/src/renderer/src/components/AuditBadge.tsx frontend/src/renderer/src/components/AppLayout.tsx frontend/src/renderer/src/styles/deck.css
git commit -m "feat(web): 状态栏安全徽章 + signal 轮询 store（30s 可调）"
```

---

### Task 10: AuditPage 重写 — 页面骨架 + ConclusionBar + StatsPanel

**Files:**
- Create: `frontend/src/renderer/src/components/audit/ListPane.tsx`（本任务先出静态骨架，Task 11 填充交互）
- Create: `frontend/src/renderer/src/components/audit/DetailPane.tsx`（同上）
- Rewrite: `frontend/src/renderer/src/components/AuditPage.tsx`
- Modify: `frontend/src/renderer/src/styles/review.css`（页面样式，全部 tokens 变量）

- [ ] **Step 1: 重写 `AuditPage.tsx`（组合壳）**

```tsx
import { useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { useI18n } from '@renderer/store/i18n'
import ListPane from './audit/ListPane'
import DetailPane from './audit/DetailPane'
import ReportDrawer from './audit/ReportDrawer'
import RulesDrawer from './audit/RulesDrawer'
import { useAuditSignal } from '@renderer/store/auditSignal'

/** 安全与审计 v2：一行结论 + 左(统计/列表)右(今日/详情) 主从。 */
export function AuditPage(): React.JSX.Element {
  const currentId = useConnections((s) => s.currentId)
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '—')
  const { t } = useI18n()
  const unread = useAuditSignal((s) => s.unread)
  const pending = useAuditSignal((s) => s.pending)
  const todayBlocked = useAuditSignal((s) => s.todayBlocked)
  const [drawer, setDrawer] = useState<'none' | 'report' | 'rules'>('none')
  const [selected, setSelected] = useState<{ kind: 'approval'; id: string } | { kind: 'entry'; id: number } | null>(null)

  if (!currentId) return <div className="mpage"><div className="mpage-empty">{t('audit.noConnection')}</div></div>

  const calm = !unread && !pending && !todayBlocked
  return (
    <div className="review kb-page audit-page au2">
      <div className="concl-bar">
        {calm
          ? <span className="cb-verdict ok">✓ {t('audit.allClearToday')}</span>
          : <span className="cb-verdict warn">
              ⚠ {t('audit.todayLine', { blocked: todayBlocked, pending })}
              {unread > 0 && <span className="cb-unread">{t('audit.unreadN', { n: unread })}</span>}
            </span>}
        <span className="cb-sub mono">{connName}</span>
        <span className="spacer" />
        <button className="rs-btn" onClick={() => setDrawer('report')}>{t('audit.reportLink')}</button>
        <button className="rs-btn" onClick={() => setDrawer('rules')}>{t('audit.rulesLink')}</button>
      </div>
      <div className="au-cols2">
        <ListPane selected={selected} onSelect={setSelected} />
        <DetailPane selected={selected} />
      </div>
      {drawer === 'report' && <ReportDrawer onClose={() => setDrawer('none')} />}
      {drawer === 'rules' && <RulesDrawer onClose={() => setDrawer('none')} />}
    </div>
  )
}
```

`ModulePages.tsx` 不动（仍 re-export AuditPage）。

- [ ] **Step 2: `audit/ListPane.tsx` 骨架**（统计+搜索框+子页签占位；数据填充在 Step 3/Task 11）

```tsx
import { useEffect, useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { auditStats, type StatBucket } from '@renderer/api/audit'
import { useI18n } from '@renderer/store/i18n'

type Scope = 'today' | '7d' | '30d'

/** 左列：三档统计 SVG 柱图 + 关键数（列表区由 Task 11 接入）。 */
export default function ListPane(props: {
  selected: unknown
  onSelect: (s: unknown) => void
}): React.JSX.Element {
  const connId = useConnections((s) => s.currentId)
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '')
  const { t } = useI18n()
  const [scope, setScope] = useState<Scope>('today')
  const [buckets, setBuckets] = useState<StatBucket[]>([])

  useEffect(() => {
    if (!connId) return
    let alive = true
    void auditStats(scope, connName).then((r) => { if (alive) setBuckets(r.buckets) }).catch(() => undefined)
    return () => { alive = false }
  }, [connId, connName, scope])

  const totals = buckets.reduce((a, b) => ({ allow: a.allow + b.allow, review: a.review + b.review, block: a.block + b.block }), { allow: 0, review: 0, block: 0 })
  const max = Math.max(1, ...buckets.map((b) => b.total))

  return (
    <div className="au-left-pane">
      <div className="sec-h"><span>{t('audit.statsTitle')}</span>
        <span className="tier-switch">
          {(['today', '7d', '30d'] as Scope[]).map((s) => (
            <button key={s} className={`tier-b${scope === s ? ' on' : ''}`} onClick={() => setScope(s)}>
              {t(`audit.scope.${s}`)}
            </button>
          ))}
        </span>
      </div>
      <div className="stat-block">
        <div className="stat-bars">
          {buckets.map((b) => (
            <div key={b.bucket} className="stat-col" title={`${b.bucket}: ${b.total}`}>
              <div className="stat-bar main" style={{ height: `${Math.round((b.total / max) * 100)}%` }} />
              {b.block > 0 && <div className="stat-bar b" style={{ height: `${Math.max(8, Math.round((b.block / max) * 100))}%` }} />}
            </div>
          ))}
        </div>
        <div className="knums">
          <span className="knum"><b>{totals.allow}</b>{t('audit.kAllow')}</span>
          <span className="knum"><b className="c-red">{totals.block}</b>{t('audit.kBlock')}</span>
          <span className="knum"><b className="c-amber">{totals.review}</b>{t('audit.kReview')}</span>
        </div>
      </div>
      <ListBody selected={props.selected} onSelect={props.onSelect} />
    </div>
  )
}

import EntryList from './EntryList'
function ListBody(selected: unknown, onSelect: (s: unknown) => void): React.JSX.Element { return <EntryList selected={selected} onSelect={onSelect} /> }
```

（注：底部两个小工具直接并入文件顶部 import，实施时把 `ListBody` 内联为 `<EntryList …/>` 即可，避免函数式包装。）

- [ ] **Step 3: `audit/DetailPane.tsx` 占位**（Task 11 实现选中详情）

```tsx
import { useI18n } from '@renderer/store/i18n'

export default function DetailPane(_props: { selected: unknown }): React.JSX.Element {
  const { t } = useI18n()
  return <div className="au-right-pane"><div className="mpage-empty">{t('common.loading')}</div></div>
}
```

- [ ] **Step 4: `review.css` 末尾追加页面样式（全部 tokens 变量）**

```css
/* ── 安全与审计 v2（au2）── */
.au2 { display: flex; flex-direction: column; height: 100%; }
.concl-bar { display: flex; align-items: center; gap: 10px; padding: 10px 16px; border-bottom: 1px solid var(--line); background: var(--void-2); }
.cb-verdict { font-size: 14px; font-weight: 600; display: inline-flex; align-items: center; gap: 8px; }
.cb-verdict.ok { color: var(--green); } .cb-verdict.warn { color: var(--amber); }
.cb-unread { background: var(--amber-dim); color: var(--amber); border-radius: 4px; padding: 0 6px; font-size: 11px; }
.cb-sub { color: var(--ink-faint); font-size: 11px; }
.au-cols2 { flex: 1; display: flex; min-height: 0; }
.au-left-pane { width: 50%; border-right: 1px solid var(--line); display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
.au-right-pane { width: 50%; overflow-y: auto; background: var(--void-1); }
.sec-h { padding: 8px 14px; font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-faint); border-bottom: 1px solid var(--line-soft); display: flex; justify-content: space-between; align-items: center; }
.tier-switch { display: inline-flex; border: 1px solid var(--line-strong); border-radius: 6px; overflow: hidden; }
.tier-b { padding: 2px 10px; font-size: 11px; color: var(--ink-dim); background: transparent; border: none; cursor: pointer; }
.tier-b.on { background: var(--void-5); color: var(--blue); }
.stat-block { padding: 12px 14px; border-bottom: 1px solid var(--line-soft); display: flex; gap: 18px; align-items: flex-end; }
.stat-bars { display: flex; gap: 5px; align-items: flex-end; height: 52px; }
.stat-col { position: relative; width: 17px; height: 100%; display: flex; align-items: flex-end; }
.stat-bar { position: absolute; bottom: 0; left: 0; right: 0; border-radius: 2px 2px 0 0; }
.stat-bar.main { background: var(--void-5); }
.stat-bar.b { background: var(--red); opacity: .85; }
.knums { display: flex; gap: 16px; font-size: 11px; color: var(--ink-dim); }
.knum b { display: block; font-size: 15px; color: var(--ink-strong); }
.c-red { color: var(--red) !important; } .c-amber { color: var(--amber) !important; }
.au-search-row { display: flex; gap: 8px; padding: 10px 14px; border-bottom: 1px solid var(--line-soft); }
.au-search { flex: 1; background: var(--void-3); border: 1px solid var(--line-strong); border-radius: 6px; padding: 5px 10px; font-size: 12px; color: var(--ink); }
.au-ltab { border: 1px solid var(--line-strong); background: transparent; color: var(--ink-dim); border-radius: 6px; padding: 2px 10px; font-size: 11px; cursor: pointer; }
.au-ltab.on { border-color: var(--blue); color: var(--blue); }
.au-row { display: flex; gap: 9px; padding: 10px 14px; border-bottom: 1px solid var(--line-soft); cursor: pointer; align-items: center; font-size: 12px; }
.au-row:hover { background: var(--void-3); }
.au-row.sel { background: var(--void-5); box-shadow: inset 3px 0 0 var(--blue); }
.au-meta { color: var(--ink-faint); margin-left: auto; font-size: 11px; white-space: nowrap; }
.tl-day { padding: 10px 16px; border-bottom: 1px solid var(--line); display: flex; gap: 10px; align-items: center; }
.ev-row { display: flex; gap: 10px; padding: 10px 16px; border-bottom: 1px dashed var(--line-soft); align-items: center; }
.ev-t { color: var(--ink-faint); font-family: var(--mono, ui-monospace, monospace); font-size: 11px; width: 44px; flex: none; }
.det-body { padding: 14px 16px; }
.det-kv { display: flex; gap: 8px; font-size: 12px; margin-bottom: 8px; }
.det-kv .k { color: var(--ink-dim); width: 72px; flex: none; }
.det-sql { background: var(--void-0); border: 1px solid var(--line-strong); border-radius: 6px; padding: 10px; font-family: var(--mono, ui-monospace, monospace); font-size: 12px; margin: 8px 0 12px; color: var(--ink); white-space: pre-wrap; word-break: break-all; }
.drawer-mask { position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 60; display: flex; justify-content: flex-end; }
.drawer-panel { width: min(520px, 90vw); background: var(--void-1); border-left: 1px solid var(--line-strong); padding: 16px; overflow-y: auto; }
```

- [ ] **Step 5: 中间态验证（DetailPane 还是占位，允许页面半成品）**

Run: `cd frontend && npm run typecheck`
Expected: 通过（EntryList 尚缺 → 本步先建最小 EntryList 见 Task 11 Step 1，或本步临时以空 div 代替并在 Task 11 替换——二选一，推荐顺序做完 Task 10+11 再统一 typecheck）

- [ ] **Step 6: Commit（与 Task 11 合并提交亦可，此处单独存档壳）**

```bash
git add frontend/src/renderer/src/components/AuditPage.tsx frontend/src/renderer/src/components/audit frontend/src/renderer/src/styles/review.css
git commit -m "feat(web): 审计页 v2 骨架——结论条+左右分栏+三档SVG统计"
```

---

### Task 11: EntryList（子页签+搜索+懒加载）与 DetailPane（今日线+处置）

**Files:**
- Create: `frontend/src/renderer/src/components/audit/EntryList.tsx`
- Fill: `frontend/src/renderer/src/components/audit/DetailPane.tsx`
- Modify: `frontend/src/renderer/src/components/audit/ListPane.tsx`（去掉占位，挂 EntryList）

- [ ] **Step 1: `EntryList.tsx` 完整实现**

```tsx
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { listAuditPage, type PageQuery } from '@renderer/api/audit'
import { VerdictBadge } from '../VerdictBadge'
import { useI18n } from '@renderer/store/i18n'

export type Sel = { kind: 'approval'; id: string } | { kind: 'entry'; id: number } | null
type Tab = 'todo' | 'all'

const PAGE = 50

interface Item { _id: number; ts: string; verdict: string; tier: string; origin: string; sql: string; ack?: string }

/** 左列下部：子页签[异常待办|全部流水] + 服务端搜索 + keyset 懒加载列表。 */
export default function EntryList(props: {
  selected: Sel
  onSelect: (s: Sel) => void
}): React.JSX.Element {
  const connId = useConnections((s) => s.currentId)
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '')
  const { t } = useI18n()
  const [tab, setTab] = useState<Tab>('todo')
  const [unreadOnly, setUnreadOnly] = useState(false)
  const [qRaw, setQRaw] = useState('')
  const [q, setQ] = useState('')
  const [items, setItems] = useState<Item[]>([])
  const [cursor, setCursor] = useState<number | null>(0)
  const [loading, setLoading] = useState(false)
  const sentinel = useRef<HTMLDivElement>(null)

  // 300ms 防抖搜索
  useEffect(() => {
    const h = window.setTimeout(() => setQ(qRaw.trim()), 300)
    return () => window.clearTimeout(h)
  }, [qRaw])

  const baseQuery = useMemo<PageQuery>(() => ({
    connection: connName || undefined,
    exception: tab === 'todo' || undefined,
    unread_only: tab === 'todo' && unreadOnly || undefined,
    q: tab === 'all' && q || undefined,
    limit: PAGE,
  }), [connName, tab, unreadOnly, q])

  const loadFirst = useCallback(() => {
    if (!connId) return
    setLoading(true)
    void listAuditPage({ ...baseQuery, cursor: 0 })
      .then((r) => { setItems(r.items as Item[]); setCursor(r.next_cursor) })
      .catch(() => { setItems([]); setCursor(null) })
      .finally(() => setLoading(false))
  }, [baseQuery, connId])

  useEffect(loadFirst, [loadFirst])

  const loadMore = useCallback(() => {
    if (!connId || cursor == null || loading) return
    setLoading(true)
    void listAuditPage({ ...baseQuery, cursor }).then((r) => {
      setItems((prev) => [...prev, ...(r.items as Item[])]); setCursor(r.next_cursor)
    }).catch(() => setCursor(null)).finally(() => setLoading(false))
  }, [baseQuery, cursor, loading, connId])

  // 滚动到底自动加载
  useEffect(() => {
    const el = sentinel.current
    if (!el) return
    const ob = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) loadMore() })
    ob.observe(el)
    return () => ob.disconnect()
  }, [loadMore])

  return (
    <>
      <div className="au-search-row">
        {tab === 'all'
          ? <input className="au-search mono" placeholder={t('audit.searchServer')} value={qRaw} onChange={(e) => setQRaw(e.target.value)} />
          : <button className={`au-ltab${unreadOnly ? ' on' : ''}`} onClick={() => setUnreadOnly((v) => !v)}>{t('audit.unreadOnly')}</button>}
        <span className="spacer" />
        <button className={`au-ltab${tab === 'todo' ? ' on' : ''}`} onClick={() => setTab('todo')}>{t('audit.tabTodo')}</button>
        <button className={`au-ltab${tab === 'all' ? ' on' : ''}`} onClick={() => setTab('all')}>{t('audit.tabAll')}</button>
      </div>
      <div style={{ overflowY: 'auto', flex: 1 }}>
        {items.map((it) => (
          <div key={it._id}
            className={`au-row${props.selected?.kind === 'entry' && props.selected.id === it._id ? ' sel' : ''}`}
            onClick={() => props.onSelect({ kind: 'entry', id: it._id })}>
            <VerdictBadge v={it.verdict} />
            <span className="mono" style={{ fontSize: 11 }}>{it.sql.slice(0, 48)}</span>
            <span className="au-meta">
              {it.verdict === 'review' ? t('verdict.review') : ''}
              {it.ack === 'unread' && it.verdict !== 'allow' ? ` · ${t('audit.unreadTag')}` : ''}
              {' '}{it.ts.slice(5, 16)}
            </span>
          </div>
        ))}
        {!loading && items.length === 0 && <div className="mpage-empty">{t('audit.empty')} 🎉</div>}
        <div ref={sentinel} style={{ height: 28 }} />
        {cursor == null && items.length > 0 && <div className="mpage-empty" style={{ fontSize: 11 }}>{t('audit.noMore')}</div>}
      </div>
    </>
  )
}
```

- [ ] **Step 2: `ListPane.tsx` 清理**——删除 Step 占位的 `ListBody` 函数与重复 import，改为顶部直接 `import EntryList from './EntryList'` 并在 JSX 末尾 `<EntryList selected={props.selected} onSelect={props.onSelect} />`；props 类型改为 `Sel`（从 `./EntryList` 导入）。

- [ ] **Step 3: `DetailPane.tsx` 完整实现**

```tsx
import { useCallback, useEffect, useState } from 'react'
import { listApprovals, approveApproval, rejectApproval, type ApprovalItem } from '@renderer/api/approvals'
import { listAuditPage, ackAudit } from '@renderer/api/audit'
import { useConnections } from '@renderer/store/connections'
import { useAuditSignal } from '@renderer/store/auditSignal'
import { VerdictBadge } from '../VerdictBadge'
import { useI18n } from '@renderer/store/i18n'
import type { Sel } from './EntryList'

interface Entry { _id: number; ts: string; verdict: string; tier: string; origin: string; sql: string; ack?: string; reasons?: { rule_id: string; message: string; message_en?: string; objects?: string[] }[] }

/** 右列：默认今日时间线；选中左列条目→完整详情+处置。 */
export default function DetailPane(props: { selected: Sel }): React.JSX.Element {
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '')
  const { t } = useI18n()
  const refreshSignal = useAuditSignal((s) => s.refresh)

  if (!props.selected) return <TodayTimeline connName={connName} />
  return props.selected.kind === 'approval'
    ? <ApprovalDetail id={props.selected.id} onChanged={refreshSignal} />
    : <EntryDetail id={props.selected.id} onChanged={refreshSignal} />

  function TodayTimeline({ connName }: { connName: string }): React.JSX.Element {
    const [rows, setRows] = useState<Entry[]>([])
    const today = new Date(); today.setHours(0, 0, 0, 0)
    useEffect(() => {
      let alive = true
      void listAuditPage({ connection: connName || undefined, from_ts: today.toISOString().slice(0, 19), limit: 50, cursor: 0 })
        .then((r) => { if (alive) setRows(r.items as Entry[]) }).catch(() => undefined)
      return () => { alive = false }
    }, [connName])
    return (
      <div className="au-right-pane">
        <div className="tl-day"><b>{t('audit.today')}</b><span className="au-meta">{t('audit.nRecords', { n: rows.length })}</span></div>
        {rows.map((r) => (
          <div key={r._id} className="ev-row">
            <span className="ev-t">{r.ts.slice(11, 16)}</span>
            <VerdictBadge v={r.verdict} />
            <span className="mono" style={{ fontSize: 11 }}>{r.sql.slice(0, 56)}</span>
          </div>
        ))}
        {rows.length === 0 && <div className="mpage-empty">{t('audit.empty')}</div>}
      </div>
    )
  }

  function ApprovalDetail({ id, onChanged }: { id: string; onChanged: () => void }): React.JSX.Element {
    const [item, setItem] = useState<ApprovalItem | null>(null)
    const [busy, setBusy] = useState(false)
    const [err, setErr] = useState<string | null>(null)
    const reload = useCallback(() => { void listApprovals().then((r) => setItem(r.items.find((x) => x.id === id) ?? null)).catch(() => undefined) }, [id])
    useEffect(reload, [reload])
    async function decide(kind: 'approve' | 'reject'): Promise<void> {
      if (!item) return
      setBusy(true); setErr(null)
      try {
        if (kind === 'approve') await approveApproval(item.id)
        else await rejectApproval(item.id, 'rejected from audit page')
        onChanged(); reload()
      } catch (e) { setErr(explain403(e)) } finally { setBusy(false) }
    }
    if (!item) return <div className="au-right-pane"><div className="mpage-empty">{t('common.loading')}</div></div>
    return (
      <div className="au-right-pane"><div className="det-body">
        <div className="det-kv"><span className="k">{t('audit.colVerdict')}</span><VerdictBadge v={item.status === 'pending' ? 'review' : item.status === 'rejected' ? 'block' : 'executed'} /><span className="au-meta mono">{item.requested_at}</span></div>
        <div className="det-sql">{item.sql}</div>
        {item.preview_rows != null && <div className="det-kv"><span className="k">{t('audit.previewRows')}</span><span className="mono">COUNT ≈ {item.preview_rows}</span></div>}
        {err && <div className="review-err">{err}</div>}
        {item.status === 'pending' ? (
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn pri" disabled={busy} onClick={() => void decide('approve')}>{t('audit.approveExec')}</button>
            <button className="btn gho" disabled={busy} onClick={() => void decide('reject')}>{t('audit.rejectBtn')}</button>
          </div>
        ) : (
          <div className="det-kv"><span className="k">{t('audit.metaStatus')}</span><span>{item.status}{item.executed_audit_id ? ` · ${t('audit.executedTag')}` : ''}</span></div>
        )}
      </div></div>
    )
  }

  function EntryDetail({ id, onChanged }: { id: number; onChanged: () => void }): React.JSX.Element {
    const connName2 = connName
    const [entry, setEntry] = useState<Entry | null>(null)
    useEffect(() => {
      let alive = true
      // 用 _id 精确取：借助 page 游标不可按 id 直查，退化为 q=sql 片段不可靠 → 直接用 list 全量过滤成本高；
      // 这里用 page(before_id=id+1, limit=1) 定位该行（id 连续且倒序第一行即目标）。
      void listAuditPage({ connection: connName2 || undefined, cursor: (id as number) + 1, limit: 1 })
        .then((r) => { if (alive) setEntry((r.items as Entry[])[0] ?? null) }).catch(() => undefined)
      return () => { alive = false }
    }, [id, connName2])
    if (!entry) return <div className="au-right-pane"><div className="mpage-empty">{t('common.loading')}</div></div>
    const canAck = entry.verdict !== 'allow' && entry.ack !== 'ack'
    return (
      <div className="au-right-pane"><div className="det-body">
        <div className="det-kv"><span className="k">{t('audit.colVerdict')}</span><VerdictBadge v={entry.verdict} /><span className="au-meta mono">{entry.ts} · {entry.origin === 'ai' ? 'AI' : t('audit.originManual')}</span></div>
        <div className="det-sql">{entry.sql}</div>
        {(entry.reasons ?? []).map((r, i) => (
          <div key={i} className="det-kv"><span className="k">{t('audit.triggerRule')}</span><span className="mono">{r.rule_id}</span><span style={{ color: 'var(--ink-dim)' }}>{r.message}</span></div>
        ))}
        {canAck && (
          <button className="btn pri" onClick={async () => { await ackAudit(entry._id); onChanged() }}>{t('audit.markAcked')}</button>
        )}
      </div></div>
    )
  }
}

function explain403(e: unknown): string {
  const msg = (e as { message?: string }).message ?? String(e)
  return msg.includes('403') ? '闸门判定为 BLOCK，无法执行' : msg
}
```

（实现备注：`explain403` 文案需走 i18n —— 用 `t('audit.gateBlockRetry')`；上面为省篇幅硬编码，落地时必须替换成 `useI18n` 取词。EntryDetail 的"按 _id 定位"若实测 `cursor=id+1` 语义不符（cursor 是严格小于），恰好成立：`WHERE id < id+1 ORDER BY id DESC LIMIT 1` 即该行本身。）

- [ ] **Step 4: 验证**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: 双绿。手动冒烟：审计页出现统计柱图/子页签/今日线；点异常待办任一行右侧出详情并可"标记已处理"（若有数据）。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/renderer/src/components/audit
git commit -m "feat(web): 审计列表(服务端搜索+keyset懒加载)与详情处置面板"
```

---

### Task 12: ReportDrawer + RulesDrawer（旧内容迁移）

**Files:**
- Create: `frontend/src/renderer/src/components/audit/ReportDrawer.tsx`
- Create: `frontend/src/renderer/src/components/audit/RulesDrawer.tsx`

- [ ] **Step 1: `ReportDrawer.tsx`** —— 把旧 AuditPage 的 egress/weekly 两块 JSX 迁移为抽屉（接口 `auditEgress(connName)`/`auditWeekly(connName)` 已存在于 api/audit.ts，直接复用；现在它们也吃 from/to，但抽屉内默认"全部"即可，不做范围选择——YAGNI）。外壳：

```tsx
import { useEffect, useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { auditEgress, auditWeekly } from '@renderer/api/audit'
import { useI18n } from '@renderer/store/i18n'

export default function ReportDrawer(props: { onClose: () => void }): React.JSX.Element {
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '')
  const { t } = useI18n()
  const [egress, setEgress] = useState<Awaited<ReturnType<typeof auditEgress>> | null>(null)
  const [weekly, setWeekly] = useState<Awaited<ReturnType<typeof auditWeekly>> | null>(null)
  useEffect(() => {
    void auditEgress(connName).then(setEgress).catch(() => undefined)
    void auditWeekly(connName).then(setWeekly).catch(() => undefined)
  }, [connName])
  return (
    <div className="drawer-mask" onClick={props.onClose}>
      <div className="drawer-panel" onClick={(e) => e.stopPropagation()}>
        <div className="sec-h"><span>{t('audit.tabEgress')}</span><button className="au-ltab" onClick={props.onClose}>✕</button></div>
        <div className="mono" style={{ fontSize: 12, padding: '8px 0' }}>
          {t('audit.egressTotal', { n: egress?.total ?? 0, byModel: Object.entries(egress?.by_model ?? {}).map(([k, v]) => `${k}:${v}`).join(' '), byMode: Object.entries(egress?.by_mode ?? {}).map(([k, v]) => `${k}:${v}`).join(' ') })}
        </div>
        <div className="sec-h"><span>{t('audit.tabWeekly')}</span></div>
        <div className="mono" style={{ fontSize: 12 }}>
          {Object.entries(weekly?.weekly ?? {}).map(([w, c]) => <span key={w} style={{ marginRight: 8 }}>{w}:{c}</span>)}
        </div>
        <div className="sec-h"><span>{t('audit.anomalyNightBatch')}</span></div>
        {(weekly?.anomalies ?? []).map((a, i) => <div key={i} className="mono" style={{ fontSize: 11, padding: '2px 0' }}>{a.ts} · {a.sql}</div>)}
      </div>
    </div>
  )
}
```

- [ ] **Step 2: `RulesDrawer.tsx`** —— 把旧 AuditPage 左栏 `.tiers` 三卡片 + `.rule-table` 规则表 JSX **原样搬入**抽屉（i18n 键全部已存在：`gate.tierRead` 系列、`gate.rule*` 系列、`audit.rulePanelSub` 等），外壳同上。此步是纯搬运，不改文案键。

- [ ] **Step 3: 验证 + Commit**

Run: `cd frontend && npm run typecheck && npm run build`

```bash
git add frontend/src/renderer/src/components/audit
git commit -m "feat(web): 报表/规则抽屉——旧五视图内容收编"
```

---

### Task 13: i18n 全量键 + AiRail 信号刷新联动 + spec 偏差补记

**Files:**
- Modify: `frontend/src/renderer/src/locales/zh-CN.ts`、`en-US.ts`
- Modify: `frontend/src/renderer/src/components/AiRail.tsx`（两处 `ev.type === 'done'`，约 L1244/L1371）
- Modify: `docs/superpowers/specs/2026-08-26-audit-security-redesign-design.md`（§4.3 偏差注记）

- [ ] **Step 1: locales 追加（zh-CN 示意，en-US 对应翻译）**

在 `audit:` 段内追加：

```typescript
    // v2
    allClear: '今日平安',
    allClearToday: '今日平安，无异常',
    unreadN: '{n} 条未读异常',
    pendingN: '{n} 条待确认',
    todayLine: '今日 {blocked} 拦截 · {pending} 待确认',
    reportLink: '报表 · 出网/周报',
    rulesLink: '闸门规则 ?',
    statsTitle: '统计',
    'scope.today': '今日',
    'scope.7d': '近7天',
    'scope.30d': '近30天',
    kAllow: '放行', kBlock: '拦截', kReview: '待确认',
    tabTodo: '异常待办', tabAll: '全部流水',
    searchServer: '搜索 SQL…（服务端检索）',
    unreadOnly: '只看未读',
    unreadTag: '未读',
    noMore: '没有更多了',
    today: '今日',
    previewRows: '影响预览',
    approveExec: '批准并执行',
    rejectBtn: '拒绝',
    markAcked: '标记已处理',
    triggerRule: '触发规则',
    executedTag: '已执行',
    gateBlockRetry: '闸门判定为 BLOCK，无法执行',
```

en-US 对应：`'Today all clear'` / `'{n} unread exceptions'` / `'{n} pending approvals'` / `'Today {blocked} blocked · {pending} pending'` / `'Reports · Egress/Weekly'` / `'Gate rules ?'` / `'Stats'` / `'Today'/'7d'/'30d'` / `'Allowed'/'Blocked'/'Pending'` / `'Exceptions'/'All entries'` / `'Search SQL… (server-side)'` / `'Unread only'` / `'unread'` / `'No more'` / `'Today'` / `'Affected preview'` / `'Approve & execute'` / `'Reject'` / `'Mark handled'` / `'Rule'` / `'executed'` / `'Gate says BLOCK; cannot execute'`。

- [ ] **Step 2: AiRail 两处 done 分支加刷新**

定位：`rg -n "ev.type === 'done'" frontend/src/renderer/src/components/AiRail.tsx`（当前 L1244、L1371 两处）。在每个分支体的首行插入：

```tsx
            void useAuditSignal.getState().refresh()
```

import 区加 `import { useAuditSignal } from '@renderer/store/auditSignal'`。

- [ ] **Step 3: spec §4.3 末尾追加偏差注记**

```markdown
> **实现偏差（2026-08-26 校准）**：当场批沿用 query.py 的 confirm_token 一次性凭证通道
> （TOCTOU 防护更完备），不走 approvals；延迟批 = AiRail「转审批」→ 审计页 decide。
> 审批终态沿用 pending/approved/rejected，approved 且带 executed_audit_id 视为已执行。
```

- [ ] **Step 4: 验证 + Commit**

Run: `cd frontend && npm run typecheck && npm run build && git add -A :/frontend/src/renderer/src/locales :/frontend/src/renderer/src/components/AiRail.tsx :/docs && git commit -m "feat(web): 审计 v2 i18n 全键 + AiRail done→signal 刷新；spec 补记偏差"`

---

### Task 14: 总验证（全链路回归 + 死规矩收尾）

- [ ] **Step 1: 后端全量**

Run: `cd backend && .venv/bin/python -m pytest -q`
Expected: 全绿

- [ ] **Step 2: 前端三件套 + e2e**

Run: `cd frontend && npm run typecheck && npm run build && npm run verify-egress 2>/dev/null || node scripts/verify-egress.mjs`
Expected: typecheck/build 绿；verify-egress 68 checks 通过（导航「安全与审计」未被改名）

- [ ] **Step 3: 重启 sidecar 并健康检查（改后端死规矩）**

```bash
lsof -ti tcp:8777 | xargs kill -9 2>/dev/null; sleep 1
cd backend && nohup .venv/bin/python -m uvicorn app.main:app --port 8777 >/tmp/tabletalk-uvicorn.log 2>&1 &
sleep 2 && curl -s http://127.0.0.1:8777/api/v1/health
```

Expected: health ok；浏览器打开 `http://127.0.0.1:8777` → 底部徽章、审计页三档统计/双主题/中英切换人工过一遍。

- [ ] **Step 4: 清理 brainstorm 临时目录（dist 内不入库，构建自净，可跳过）**

```bash
rm -rf frontend/dist/brainstorm
```

- [ ] **Step 5: 最终提交（如有遗漏散件）**

```bash
git status --short && git add -A && git commit -m "chore(audit): 安全与审计 v2 收尾" || true
```

---

## 自审记录（writing-plans Self-Review）

1. **Spec coverage**：§2 IA→T10–12；§3 审批模型→T6/T11（偏差已在 T13 补记）；§4.1 ack 表→T1、approvals 字段→T6；§4.2 五端点→T2/T3/T4/T5/T8(ack)；§5 组件→T9–11、轮询常量→T9、i18n/theme→T12–13；§6 错误处理→T9(静默)/T11(403 文案/409 由后端 404 兜底)；§7 测试→各 Task 内嵌 + T14；§8 Non-goals 未引入。
2. **Placeholder 扫描**：无 TBD/TODO；T6 preview_rows 键名留了一处"实现时 REPL 确认"，已给具体命令式指引，属可执行检查而非空洞指令。
3. **类型一致性**：`Sel` 在 EntryList 定义并被 AuditPage/ListPane/DetailPane 引用一致；`page()` 返回 `{items,has_more}` 与端点 `next_cursor` 映射一致；`stats_buckets(connection,since_ts,fmt)` 与端点调用签名一致。
