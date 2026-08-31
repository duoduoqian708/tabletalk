"""T10 L3 行为层测试：日志挖掘 / 边加权 / few-shot。"""
from __future__ import annotations

from app.knowledge.behavior.fewshot import FewShotStore
from app.knowledge.behavior.log_mining import mine_join_edges
from app.knowledge.behavior.weighting import bump_weights
from app.knowledge.graph.model import GraphEdge


# ---------- 日志挖掘 ----------

def test_mine_join_edges_basic():
    rows = [{"sql": "SELECT * FROM orders JOIN users ON orders.user_id = users.id", "connection": "c1"}]
    edges = mine_join_edges(rows)
    assert len(edges) == 1
    e = edges[0]
    assert e.source_table == "orders" and e.target_table == "users"
    assert e.cols == [("user_id", "id")]
    assert e.confidence == 0.9
    assert e.provenance == "query_log"


def test_mine_skips_unparseable():
    rows = [{"sql": "NOT VALID SQL {{{", "connection": "c1"},
            {"sql": "SELECT 1", "connection": "c1"}]
    edges = mine_join_edges(rows)
    assert edges == []  # 非法跳过，不中断


def test_mine_unqualified_cols_skipped():
    """join 条件列无表限定 → 无法归属 → 跳过（宁缺勿错）。"""
    rows = [{"sql": "SELECT * FROM a JOIN b ON id = id", "connection": "c1"}]
    assert mine_join_edges(rows) == []


def test_mine_dedup():
    rows = [
        {"sql": "SELECT * FROM orders JOIN users ON orders.user_id = users.id"},
        {"sql": "SELECT * FROM orders o JOIN users u ON o.user_id = u.id"},
    ]
    edges = mine_join_edges(rows)
    assert len(edges) == 1  # 同列对去重


# ---------- 边加权 ----------

def test_bump_weights():
    e = GraphEdge(source_table="orders", target_table="users", cols=[("user_id", "id")], weight=1.0)
    n = bump_weights([e], [e], factor=1.1)
    assert n == 1
    assert e.weight == 1.1


def test_bump_weights_unused():
    e = GraphEdge(source_table="orders", target_table="users", cols=[("user_id", "id")])
    other = GraphEdge(source_table="orders", target_table="customers", cols=[("customer_id", "id")])
    assert bump_weights([e], [other]) == 0
    assert e.weight == 1.0


def test_weight_cap():
    e = GraphEdge(source_table="orders", target_table="users", cols=[("user_id", "id")], weight=9.9)
    for _ in range(5):
        bump_weights([e], [e], factor=1.5)
    assert e.weight == 10.0  # 封顶


# ---------- few-shot ----------

def test_fewshot_add_recall():
    fs = FewShotStore()
    fs.add("c1", "查一下最近的退货记录", "SELECT * FROM returns", ["returns.id = orders.id"])
    fs.add("c1", "查一下最近的订单", "SELECT * FROM orders", ["orders.id = customers.id"])
    hits = fs.recall("c1", "查一下最近的退货记录有哪些", k=3)
    assert len(hits) >= 1
    assert hits[0]["question"] == "查一下最近的退货记录"  # 相同问题优先


def test_fewshot_recall_no_match():
    fs = FewShotStore()
    fs.add("c1", "查一下订单", "SELECT 1", [])
    assert fs.recall("c1", "今天天气怎么样", k=3) == []


def test_fewshot_limit():
    fs = FewShotStore()
    fs._items["c1"] = []
    for i in range(510):
        fs.add("c1", f"问题{i}", f"SELECT {i}", [])
    assert len(fs._items["c1"]) == 500  # 上限清理


def test_fewshot_limit_configurable(monkeypatch):
    """R5：上限可经 TABLETALK_FEWSHOT_MAX 配置。"""
    from app.knowledge.behavior.fewshot import _max_per_conn

    monkeypatch.setenv("TABLETALK_FEWSHOT_MAX", "10")
    assert _max_per_conn() == 10
    fs = FewShotStore()
    fs._items["c1"] = []
    for i in range(20):
        fs.add("c1", f"问题{i}", f"SELECT {i}", [])
    assert len(fs._items["c1"]) == 10


def test_fewshot_persist():
    fs = FewShotStore()
    fs.add("c1", "查订单", "SELECT * FROM orders", ["orders.id = 1"])
    d = fs.dump("c1")
    fs2 = FewShotStore()
    fs2.load("c1", d)
    assert len(fs2.recall("c1", "查订单", k=1)) == 1


# ---------- R7：跨来源加权 + 门面回灌 ----------

def test_bump_weights_kind_agnostic():
    """存量 fk 边被 query_log used 边加权（kind 不进匹配键，T10 跨来源）。"""
    import pytest as _pytest
    stored = [{"from": "orders", "from_col": "user_id", "to": "users", "to_col": "id",
               "kind": "fk", "weight": 1.0, "guard": None}]
    used = [{"from": "orders", "from_col": "user_id", "to": "users", "to_col": "id",
             "kind": "query_log", "weight": 1.0, "guard": None}]
    assert bump_weights(stored, used) == 1
    assert stored[0]["weight"] == _pytest.approx(1.1)


async def test_record_query_success_weights_and_fewshot(app_state, conn_id):
    """查询成功回灌：join 边加权 + few-shot 入库；失败静默。"""
    kb = app_state.knowledge
    kb.semantic_store._schema[conn_id] = {
        "tables": [{"name": "orders"}, {"name": "users"}],
        "columns": [{"table": "orders", "name": "user_id"}, {"table": "users", "name": "id"}],
    }
    kb.graph_store._graph[conn_id] = {"edges": [{
        "from": "orders", "from_col": "user_id", "to": "users", "to_col": "id",
        "kind": "fk", "weight": 1.0, "confidence": 1.0, "provenance": "declared_fk",
        "guard": None, "cols": [["user_id", "id"]], "cardinality": "n:1", "reason": "",
    }]}
    out = kb.record_query_success(
        conn_id, "SELECT * FROM orders JOIN users ON orders.user_id = users.id", "用户订单查询")
    assert out["weighted"] == 1
    assert kb.graph_store._graph[conn_id]["edges"][0]["weight"] > 1.0
    assert out["fewshot"] is True
    hits = kb.fewshot_store.recall(conn_id, "用户订单查询", k=3)
    assert len(hits) == 1 and hits[0]["sql"].startswith("SELECT")
    # 无 question → 只加权不入 few-shot
    kb.record_query_success(conn_id, "SELECT 1", None)
    assert len(kb.fewshot_store._items.get(conn_id, [])) == 1


async def test_apply_query_log_edges_dedup_and_phantom(app_state, conn_id):
    """日志挖掘追加：幻觉表丢弃、按列对去重幂等。"""
    kb = app_state.knowledge
    kb.semantic_store._schema[conn_id] = {
        "tables": [{"name": "orders"}, {"name": "users"}],
        "columns": [{"table": "orders", "name": "user_id"}, {"table": "users", "name": "id"}],
    }
    kb.graph_store._graph[conn_id] = {"edges": []}
    rows = [
        {"sql": "SELECT * FROM orders JOIN users ON orders.user_id = users.id"},
        {"sql": "SELECT * FROM orders JOIN ghosts ON orders.gid = ghosts.id"},  # 幻觉表
    ]
    assert kb.apply_query_log_edges(conn_id, rows) == 1
    # 2026-08-31 修订：挖掘结果投递 draft 队列（不直接进正式图）
    drafts = kb.llm_graph_edges(conn_id)
    assert len(drafts) == 1
    assert drafts[0]["kind"] == "query_log"
    assert drafts[0]["from_table"] == "orders" and drafts[0]["to_table"] == "users"
    # 确认后进入正式图
    assert kb.confirm_graph_edges(conn_id) == 1
    edges = kb.graph_store._graph[conn_id]["edges"]
    assert edges[0]["kind"] == "query_log" and edges[0]["from"] == "orders" and edges[0]["to"] == "users"
    # 幂等：重复 tick 不重复加（已确认边不再提案）
    assert kb.apply_query_log_edges(conn_id, rows) == 0


# ---------- R15/H2：SyncLoop.tick 结构无变化仍挖掘 ----------

async def test_sync_tick_mines_without_schema_change(app_state, conn_id, tmp_path, monkeypatch):
    """日志挖掘脱离 needs_sync 短路：结构指纹无变化时，tick 仍挖掘新审计行（T10 定时触发）。"""
    import sqlite3

    from app.knowledge.jobs import SyncLoop

    kb = app_state.knowledge
    kb.semantic_store._schema[conn_id] = {
        "tables": [{"name": "orders"}, {"name": "users"}],
        "columns": [{"table": "orders", "name": "user_id"}, {"table": "users", "name": "id"}],
    }
    kb.graph_store._graph[conn_id] = {"edges": []}

    cfg = app_state.connections.get(conn_id)
    audit = app_state.audit
    audit.log(connection=cfg.name, origin="manual", tier="query", verdict="allow",
              status="ok", sql="SELECT * FROM orders JOIN users ON orders.user_id = users.id")

    # 强制"结构无变化"路径（指纹比对短路）；挖掘必须仍执行
    monkeypatch.setattr(kb, "needs_sync", lambda *a, **k: False)

    loop = SyncLoop()
    await loop.tick()

    # 2026-08-31 修订：挖掘结果投递 draft 队列，确认后进正式图
    drafts = kb.llm_graph_edges(conn_id)
    assert any(e["kind"] == "query_log" for e in drafts), drafts
    assert kb.confirm_graph_edges(conn_id) >= 1
    edges = kb.graph_store._graph[conn_id]["edges"]
    assert any(e["kind"] == "query_log" for e in edges), edges

    # 幂等：同审计行再次 tick 不重复加（已确认边不再提案）
    await loop.tick()
    assert len([e for e in kb.graph_store._graph[conn_id]["edges"] if e["kind"] == "query_log"]) == 1


# ---------- R13：挖掘 schema 校验（幻影表） ----------

def test_mine_drops_phantom_tables():
    """T10 §4#4：join 不存在的表 → 边被丢弃（schema 校验）。"""
    rows = [{"sql": "SELECT * FROM orders JOIN ghosts ON orders.gid = ghosts.id"}]
    schema = {"columns": [{"table": "orders", "name": "gid"}, {"table": "users", "name": "id"}]}
    assert mine_join_edges(rows, schema=schema) == []
    # 真实表对保留；无 schema 时不校验（兼容直接挖掘调用）
    rows2 = [{"sql": "SELECT * FROM orders JOIN users ON orders.user_id = users.id"}]
    schema2 = {"columns": [{"table": "orders", "name": "user_id"}, {"table": "users", "name": "id"}]}
    assert len(mine_join_edges(rows2, schema=schema2)) == 1
    assert len(mine_join_edges(rows2)) == 1
