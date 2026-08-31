"""T7 概念字典测试：CRUD / 冲突校验 / values 解析 / 静态常量 / 防漂移 / 持久化 / kb_read。"""
from __future__ import annotations

from app.ai.tools.kb_read import _kb_read
from app.knowledge.facade import KnowledgeBase
from app.knowledge.semantic.concepts import Concept, ConceptStore


def _region_concept() -> Concept:
    return Concept(
        name="地区",
        canonical_enum=[{"code": "EAST", "label": "华东"}, {"code": "WEST", "label": "华南"}],
        members=[{"table": "orders", "column": "region", "mapping": "identity"}],
        status="draft",
    )


# ---------- CRUD ----------

def test_upsert_and_get():
    cs = ConceptStore()
    assert cs.upsert("c1", _region_concept()) is True
    c = cs.get("c1", "地区")
    assert c is not None
    assert c.canonical_enum[0]["code"] == "EAST"


def test_get_for_column():
    cs = ConceptStore()
    cs.upsert("c1", _region_concept())
    c = cs.get_for_column("c1", "orders", "region")
    assert c is not None and c.name == "地区"
    assert cs.get_for_column("c1", "orders", "amount") is None


def test_confirm_reject():
    cs = ConceptStore()
    cs.upsert("c1", _region_concept())
    assert cs.confirm("c1", "地区") is True
    assert cs.get("c1", "地区").status == "confirmed"
    assert cs.reject("c1", "地区") is True
    assert cs.get("c1", "地区") is None


def test_member_conflict_rejected():
    cs = ConceptStore()
    cs.upsert("c1", _region_concept())
    other = Concept(name="地域", canonical_enum=[{"code": "E", "label": "东"}],
                    members=[{"table": "orders", "column": "region", "mapping": "identity"}])
    assert cs.upsert("c1", other) is False  # orders.region 已被"地区"占用


def test_confirmed_not_downgraded():
    """同名覆盖不把 confirmed 降回 draft。"""
    cs = ConceptStore()
    cs.upsert("c1", _region_concept())
    cs.confirm("c1", "地区")
    cs.upsert("c1", _region_concept())  # draft 覆盖
    assert cs.get("c1", "地区").status == "confirmed"


# ---------- values 解析 ----------

def test_values_parse():
    out = ConceptStore.parse_values_to_candidates("P=待付款;S=已发货；R：已退货")
    assert out == [
        {"code": "P", "label": "待付款"},
        {"code": "S", "label": "已发货"},
        {"code": "R", "label": "已退货"},
    ]


def test_values_parse_empty():
    assert ConceptStore.parse_values_to_candidates("") == []
    assert ConceptStore.parse_values_to_candidates("乱七八糟") == []


# ---------- 静态业务常量 ----------

def test_constant_kind():
    cs = ConceptStore()
    c = Concept(name="const.tax_rate", kind="constant",
                canonical_enum=[{"code": "0.13", "label": "增值税率"}],
                members=[])
    assert cs.upsert("c1", c) is True
    got = cs.get("c1", "const.tax_rate")
    assert got.kind == "constant"


# ---------- 防漂移 ----------

def test_drift_detection():
    cs = ConceptStore()
    cs.upsert("c1", _region_concept())
    new = cs.detect_drift("c1", "orders", "region", ["EAST", "WEST", "NORTH"])
    assert new == ["NORTH"]  # 新值提示，不自动写


# ---------- R15/H3：防漂移接入 SyncLoop（定时采样比对） ----------

async def test_check_concept_drift(app_state, tmp_path):
    """tick 防漂移：confirmed 概念成员列 DISTINCT 采样 → 返回新值（仅提示，不自动写枚举）。"""
    import sqlite3

    from app.knowledge.jobs import SyncLoop
    from app.knowledge.semantic.concepts import Concept

    p = tmp_path / "drift.db"
    con = sqlite3.connect(str(p))
    con.executescript(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT);"
        "INSERT INTO orders VALUES (1, 'P'), (2, 'S'), (3, 'R');"
    )
    con.commit()
    con.close()
    c = app_state.connections.create({"name": "drift", "dialect": "sqlite", "file": str(p)})
    app_state.connections.set_kb_status(c.id, "ready")
    kb = app_state.knowledge
    kb.semantic_store._schema[c.id] = {
        "tables": [{"name": "orders"}],
        "columns": [{"table": "orders", "name": "status"}],
    }
    kb.concept_store.upsert(c.id, Concept(
        name="订单状态",
        canonical_enum=[{"code": "P", "label": "P"}, {"code": "S", "label": "S"}],
        members=[{"table": "orders", "column": "status", "mapping": "code"}],
        status="confirmed",
    ))
    schema = kb.semantic_store._schema[c.id]
    drift = await SyncLoop()._check_concept_drift(app_state, c.id, schema)
    assert drift == {"订单状态": ["R"]}
    # 铁律：不自动写（canonical_enum 仍是 P/S）
    assert kb.concept_store.get(c.id, "订单状态").canonical_enum == [
        {"code": "P", "label": "P"}, {"code": "S", "label": "S"}]


async def test_check_concept_drift_skips_draft(app_state, tmp_path):
    """draft（未确认）概念不参与防漂移采样。"""
    import sqlite3

    from app.knowledge.jobs import SyncLoop
    from app.knowledge.semantic.concepts import Concept

    p = tmp_path / "drift2.db"
    con = sqlite3.connect(str(p))
    con.executescript(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT);"
        "INSERT INTO orders VALUES (1, 'X');"
    )
    con.commit()
    con.close()
    c = app_state.connections.create({"name": "drift2", "dialect": "sqlite", "file": str(p)})
    app_state.connections.set_kb_status(c.id, "ready")
    kb = app_state.knowledge
    kb.semantic_store._schema[c.id] = {
        "tables": [{"name": "orders"}],
        "columns": [{"table": "orders", "name": "status"}],
    }
    kb.concept_store.upsert(c.id, Concept(
        name="地区", canonical_enum=[{"code": "E", "label": "东"}],
        members=[{"table": "orders", "column": "status", "mapping": "code"}],
        status="draft",
    ))
    drift = await SyncLoop()._check_concept_drift(
        app_state, c.id, kb.semantic_store._schema[c.id])
    assert drift == {}


# ---------- 持久化 ----------

async def test_persist_across_instances(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), enable_ai_annotation=False)
    kb.concept_store.upsert("c1", _region_concept())
    kb._save_conn("c1")

    kb2 = KnowledgeBase(tmp_path)
    kb2._load_conn("c1")
    assert kb2.concept_store.get("c1", "地区") is not None


# ---------- kb_read 工具 ----------

async def test_kb_read_concept(app_state, conn_id):
    kb = app_state.knowledge
    await kb.build(conn_id, _schema(), enable_ai_annotation=False)
    kb.concept_store.upsert(conn_id, _region_concept())

    out = await _kb_read(app_state, {"query_type": "concept", "table_name": "orders", "column": "region"}, conn_id)
    assert out.result["concept"] is not None
    assert out.result["concept"]["name"] == "地区"

    out2 = await _kb_read(app_state, {"query_type": "concept", "keyword": "地区"}, conn_id)
    assert len(out2.result["concepts"]) == 1


def _schema() -> dict:
    return {
        "tables": [
            {"name": "orders", "kind": "table", "comment": "", "column_count": 2},
            {"name": "customers", "kind": "table", "comment": "", "column_count": 1},
        ],
        "columns": [
            {"table": "orders", "name": "id", "type": "int", "nullable": True, "pk": True, "fk": False, "default": None, "comment": ""},
            {"table": "orders", "name": "customer_id", "type": "int", "nullable": True, "pk": False, "fk": True, "default": None, "comment": ""},
            {"table": "customers", "name": "id", "type": "int", "nullable": True, "pk": True, "fk": False, "default": None, "comment": ""},
        ],
        "foreign_keys": [
            {"table": "orders", "column": "customer_id", "ref_table": "customers", "ref_column": "id"},
        ],
    }


# ---------- R12：schema 校验 + values→候选接入 ----------

def test_upsert_schema_validation():
    """T7 §5#5：成员列引用的 (table,column) 不存在于 schema → 拒绝。"""
    from app.knowledge.semantic.concepts import Concept
    store = ConceptStore()
    schema = {"columns": [{"table": "orders", "name": "status"}]}
    ok = Concept(name="订单状态",
                 members=[{"table": "orders", "column": "status", "mapping": "code"}])
    assert store.upsert("c1", ok, schema) is True
    ghost = Concept(name="幽灵概念",
                    members=[{"table": "ghost", "column": "id", "mapping": "code"}])
    assert store.upsert("c1", ghost, schema) is False
    # 无 schema 时不校验（兼容直接 upsert 的调用方）
    assert store.upsert("c1", ghost) is True


def test_ingest_values_candidates(app_state, conn_id):
    """values 平铺串 → 概念条目候选（draft, source=sampling）。"""
    class _CI:
        def __init__(self, values):
            self.values = values

    class _TK:
        def __init__(self, cols):
            self.columns = cols

    kb = app_state.knowledge
    kb.semantic_store._tables[conn_id] = {
        "orders": _TK({"status": _CI("P=待付款;S=已发货")}),
    }
    schema = {"columns": [{"table": "orders", "name": "status"}]}
    n = kb.build_service.ingest_values_candidates(kb, conn_id, schema)
    assert n == 1
    cs = kb.concept_store.list(conn_id)
    assert len(cs) == 1 and cs[0].name == "orders.status"
    assert cs[0].status == "draft" and cs[0].source == "sampling"
    assert cs[0].canonical_enum == [{"code": "P", "label": "待付款"}, {"code": "S", "label": "已发货"}]
    # 同名再跑不重复（覆盖），confirmed 状态不降级
    kb.concept_store.confirm(conn_id, "orders.status")
    kb.build_service.ingest_values_candidates(kb, conn_id, schema)
    assert len(kb.concept_store.list(conn_id)) == 1
    assert kb.concept_store.get(conn_id, "orders.status").status == "confirmed"
