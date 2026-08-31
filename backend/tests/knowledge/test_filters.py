"""T8 表级过滤器 + 会话变量池测试。"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.knowledge.filters import (
    SESSION_VARS,
    FilterStore,
    TableFilter,
    prepare_query_sql,
    resolve_session_var,
    resolve_session_vars_in_sql,
    session_vars_prompt,
)


def _schema_with_filters() -> dict:
    return {
        "tables": [
            {"name": "orders", "kind": "table", "comment": "", "column_count": 3},
            {"name": "system_config", "kind": "table", "comment": "", "column_count": 1},
        ],
        "columns": [
            {"table": "orders", "name": "id", "type": "int", "nullable": True, "pk": True, "fk": False, "default": None, "comment": ""},
            {"table": "orders", "name": "is_deleted", "type": "int", "nullable": True, "pk": False, "fk": False, "default": None, "comment": ""},
            {"table": "orders", "name": "tenant_id", "type": "int", "nullable": True, "pk": False, "fk": False, "default": None, "comment": ""},
            {"table": "system_config", "name": "key", "type": "text", "nullable": True, "pk": False, "fk": False, "default": None, "comment": ""},
        ],
        "foreign_keys": [],
    }


# ---------- 检测 ----------

def test_detect_soft_delete():
    cands = FilterStore().detect_candidates(_schema_with_filters())
    soft = [c for c in cands if c.predicate == "is_deleted = 0"]
    assert len(soft) == 1 and soft[0].table == "orders"
    assert soft[0].status == "draft"


def test_detect_tenant():
    cands = FilterStore().detect_candidates(_schema_with_filters())
    tenant = [c for c in cands if c.predicate == "tenant_id = :current_tenant"]
    assert len(tenant) == 1
    assert tenant[0].scope == "connection_default"


# ---------- 查询合并 ----------

def test_get_filters_merged():
    fs = FilterStore()
    fs._filters["c1"] = {
        "orders": TableFilter(table="orders", predicate="is_deleted = 0", status="confirmed"),
        "system_config": TableFilter(table="system_config", predicate="tenant_id = :current_tenant",
                                     scope="connection_default", status="confirmed"),
        "audit_log": TableFilter(table="audit_log", predicate="", scope="exempt", status="confirmed"),
    }
    out = fs.get_filters("c1", {"orders", "audit_log", "unknown"})
    assert out.get("orders") == ["is_deleted = 0"]
    assert "audit_log" not in out        # exempt 排除
    assert "unknown" not in out          # 无过滤器


def test_confirm_reject():
    fs = FilterStore()
    fs._filters["c1"] = {"orders": TableFilter(table="orders", predicate="is_deleted = 0")}
    assert fs.confirm("c1", "orders") is True
    assert fs._filters["c1"]["orders"].status == "confirmed"
    assert fs.reject("c1", "orders") is True


# ---------- 会话变量 ----------

def test_resolve_session_var():
    assert resolve_session_var(":current_tenant", {"current_tenant": 7}) == "7"
    assert resolve_session_var(":current_user", {"current_user": 3}) == "3"


def test_resolve_current_date():
    assert resolve_session_var(":current_date", {}) == datetime.now().strftime("%Y-%m-%d")


def test_resolve_unknown_raises():
    with pytest.raises(KeyError):
        resolve_session_var(":current_foo", {})
    with pytest.raises(KeyError):
        resolve_session_var(":current_tenant", {})  # ctx 缺值


def test_session_vars_prompt():
    text = session_vars_prompt()
    assert ":current_tenant" in text
    assert "当前租户ID" in text
    assert "7" not in text  # 不含真实值


# ---------- R6：执行链（FilterStore.add / 会话变量替换 / prepare_query_sql） ----------

def test_add_filter():
    store = FilterStore()
    f = store.add("c1", "orders", "is_deleted = 0")
    assert f.status == "draft"
    # 覆盖语义：同名表再 add 覆盖
    store.add("c1", "orders", "deleted = 0", scope="exempt", status="confirmed")
    assert store._filters["c1"]["orders"].predicate == "deleted = 0"
    assert store._filters["c1"]["orders"].scope == "exempt"


def test_resolve_session_vars_in_sql():
    sql = ("SELECT * FROM t WHERE tenant_id = :current_tenant "
           "AND note = 'a:current_tenant' AND d = :current_date")
    out = resolve_session_vars_in_sql(sql, {"current_tenant": 7})
    assert "tenant_id = '7'" in out
    assert "a:current_tenant" in out          # 字符串字面量绝不误替换
    assert ":current_date" not in out         # 时钟变量由系统时间替换
    # 未配置 → 保留占位符（执行层报错可见，不注入错误值）
    out2 = resolve_session_vars_in_sql("SELECT * FROM t WHERE a = :current_tenant", {})
    assert ":current_tenant" in out2


# ---------- R15/H1：构建接入（ingest_filter_candidates） ----------

def _schema_orders_dual() -> dict:
    """orders 表同时含软删除列 + 租户列（一表两条候选，应合并为一条）。"""
    return {
        "tables": [{"name": "orders", "kind": "table", "comment": "", "column_count": 3}],
        "columns": [
            {"table": "orders", "name": "id", "type": "int", "pk": True, "fk": False},
            {"table": "orders", "name": "is_deleted", "type": "int", "pk": False, "fk": False},
            {"table": "orders", "name": "tenant_id", "type": "int", "pk": False, "fk": False},
        ],
        "foreign_keys": [],
    }


def test_ingest_filter_candidates(app_state, conn_id):
    """构建接入：结构检测候选落库（draft，同表多候选 AND 合并）；confirmed 不降级不被覆盖。"""
    kb = app_state.knowledge
    fs = kb.filter_store
    schema = _schema_orders_dual()
    n = kb.build_service.ingest_filter_candidates(kb, conn_id, schema)
    assert n == 1
    f = fs._filters[conn_id]["orders"]
    assert f.status == "draft"
    assert "is_deleted = 0" in f.predicate
    assert "tenant_id = :current_tenant" in f.predicate
    # confirmed 不降级、谓词不被重建覆盖（人工已确认的保留）
    fs.confirm(conn_id, "orders")
    kb.build_service.ingest_filter_candidates(kb, conn_id, schema)
    assert fs._filters[conn_id]["orders"].status == "confirmed"
    assert fs._filters[conn_id]["orders"].predicate == "is_deleted = 0 AND tenant_id = :current_tenant"


def test_ingest_filter_candidates_no_hit(app_state, conn_id):
    """无候选列（纯 ID/数值表）→ 不落库、返回 0。"""
    kb = app_state.knowledge
    schema = {
        "tables": [{"name": "products", "kind": "table", "comment": "", "column_count": 1}],
        "columns": [{"table": "products", "name": "id", "type": "int", "pk": True, "fk": False}],
        "foreign_keys": [],
    }
    assert kb.build_service.ingest_filter_candidates(kb, conn_id, schema) == 0
    assert conn_id not in kb.filter_store._filters


def test_prepare_query_sql(app_state, tmp_path):
    """S1：端到端加工只做会话变量填充——过滤器已从执行层移除（纯知识形态）。"""
    import sqlite3
    from app.knowledge.filters import prepare_query_sql as _pqs

    p = tmp_path / "t8.db"
    con = sqlite3.connect(str(p))
    con.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, is_deleted INTEGER DEFAULT 0, tenant_id INTEGER);")
    con.commit()
    con.close()
    c = app_state.connections.create(
        {"name": "t8", "dialect": "sqlite", "file": str(p),
         "session_vars": {"current_tenant": 7}}
    )
    try:
        fs: FilterStore = app_state.knowledge.filter_store
        fs.add(c.id, "orders", "is_deleted = 0 AND tenant_id = :current_tenant")
        fs.confirm(c.id, "orders")
        sql = "SELECT * FROM orders WHERE tenant_id = :current_tenant"
        out = _pqs(app_state, c.id, sql)
        assert ":current_tenant" not in out      # 主 SQL 变量已替换
        assert "tenant_id = '7'" in out
        assert "is_deleted" not in out           # S1：执行层不再注入过滤器
        # 无变量 SQL：原样
        assert _pqs(app_state, c.id, "SELECT 1") == "SELECT 1"
    finally:
        app_state.knowledge.filter_store._filters.pop(c.id, None)
        app_state.connections.delete(c.id)


# ---------- S2-3：会话变量可配置（连接自定义变量） ----------

def test_session_vars_prompt_includes_custom(app_state, conn_id):
    """S2-3：连接配置自定义变量（元信息格式）并入清单；内置不变。"""
    from app.knowledge.filters import session_vars_prompt
    try:
        app_state.connections.get(conn_id).session_vars = {
            "current_branch": {"value": 3, "type": "int", "description": "当前门店号"},
        }
    except Exception:
        pass
    text = session_vars_prompt(app_state, conn_id)
    assert ":current_tenant" in text            # 内置保留
    assert ":current_branch" in text            # 自定义出现
    assert "当前门店号" in text


async def test_resolve_custom_session_var(app_state, conn_id):
    """S2-3：自定义变量（连接配置有值）可替换；未配置的仍拒绝。"""
    from app.knowledge.filters import prepare_query_sql, resolve_session_var
    try:
        app_state.connections.get(conn_id).session_vars = {
            "current_branch": {"value": 3, "type": "int", "description": "门店号"},
        }
    except Exception:
        pass
    ctx = {"current_branch": 3}
    assert resolve_session_var(":current_branch", ctx) == "3"
    out = prepare_query_sql(app_state, conn_id,
                            "SELECT * FROM orders WHERE branch_id = :current_branch")
    assert "branch_id = '3'" in out
    # 未配置变量 → 拒绝（不注入错误值）
    try:
        resolve_session_var(":current_fake", {})
        assert False, "应拒绝未配置变量"
    except KeyError:
        pass
