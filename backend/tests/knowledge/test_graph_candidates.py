"""程序候选生成（阶段三第 2 步输入之一）：引用后缀 + 单复数变体 + 类型族校验。"""
from __future__ import annotations

from app.knowledge.annotator import (
    _REF_SUFFIXES,
    _generate_candidate_pairs,
    _table_name_variants,
    _type_family,
)


def _schema(tables: list[dict], columns: list[dict]) -> dict:
    return {"tables": tables, "columns": columns, "foreign_keys": []}


def _pairs(schema: dict) -> list[tuple[str, str, str, str]]:
    return [(c["from_table"], c["from_col"], c["to_table"], c["to_col"])
            for c in _generate_candidate_pairs(schema)]


def test_type_family():
    assert _type_family("INT") == "int"
    assert _type_family("VARCHAR(64)") == "text"
    assert _type_family("TEXT") == "text"
    assert _type_family("DATE") is None
    assert _type_family(None) is None


def test_table_name_variants():
    assert _table_name_variants("customer") >= {"customer", "customers"}
    assert _table_name_variants("category") >= {"category", "categories"}
    assert _table_name_variants("addresses") >= {"addresses", "address"}
    assert _table_name_variants("bus") >= {"bus", "buses"}


def test_suffix_set():
    assert _REF_SUFFIXES == ("_id", "_code", "_no", "_num", "_key", "_ref")


def test_id_suffix_and_plural_table():
    """customer_id(INT) → customers.id（复数表名 + INT 族主键匹配）。"""
    schema = _schema(
        tables=[{"name": "customers", "column_count": 2}, {"name": "orders", "column_count": 2}],
        columns=[
            {"table": "customers", "name": "id", "type": "INT", "pk": True, "fk": False},
            {"table": "orders", "name": "id", "type": "INT", "pk": True, "fk": False},
            {"table": "orders", "name": "customer_id", "type": "INT", "pk": False, "fk": False},
        ],
    )
    assert ("orders", "customer_id", "customers", "id") in _pairs(schema)


def test_code_suffix_matches_text_column():
    """customer_code(VARCHAR) → customers.code（文本引用指向文本列，非 INT 主键）。"""
    schema = _schema(
        tables=[{"name": "customers", "column_count": 2}, {"name": "orders", "column_count": 2}],
        columns=[
            {"table": "customers", "name": "id", "type": "INT", "pk": True, "fk": False},
            {"table": "customers", "name": "code", "type": "VARCHAR(32)", "pk": False, "fk": False},
            {"table": "orders", "name": "id", "type": "INT", "pk": True, "fk": False},
            {"table": "orders", "name": "customer_code", "type": "VARCHAR(32)", "pk": False, "fk": False},
        ],
    )
    pairs = _pairs(schema)
    assert ("orders", "customer_code", "customers", "code") in pairs
    # 同时 INT 引用仍配主键
    schema["columns"].append(
        {"table": "orders", "name": "customer_id", "type": "INT", "pk": False, "fk": False}
    )
    pairs2 = _pairs(schema)
    assert ("orders", "customer_id", "customers", "id") in pairs2


def test_y_to_ies_variant():
    """category_id → categories（y→ies 变体）。"""
    schema = _schema(
        tables=[{"name": "categories", "column_count": 2}, {"name": "products", "column_count": 2}],
        columns=[
            {"table": "categories", "name": "id", "type": "INT", "pk": True, "fk": False},
            {"table": "products", "name": "id", "type": "INT", "pk": True, "fk": False},
            {"table": "products", "name": "category_id", "type": "INT", "pk": False, "fk": False},
        ],
    )
    assert ("products", "category_id", "categories", "id") in _pairs(schema)


def test_type_mismatch_rejected():
    """文本引用列 vs INT 主键 → 无候选（不同类型族不配对）。"""
    schema = _schema(
        tables=[{"name": "customers", "column_count": 2}, {"name": "orders", "column_count": 2}],
        columns=[
            {"table": "customers", "name": "id", "type": "INT", "pk": True, "fk": False},
            {"table": "orders", "name": "id", "type": "INT", "pk": True, "fk": False},
            {"table": "orders", "name": "customer_ref", "type": "VARCHAR(32)", "pk": False, "fk": False},
        ],
    )
    assert _pairs(schema) == []


def test_generic_base_skipped():
    """status_code → base=status 在通用名单 → 不生成候选（即使存在 statuses 表）。"""
    schema = _schema(
        tables=[{"name": "statuses", "column_count": 2}, {"name": "orders", "column_count": 2}],
        columns=[
            {"table": "statuses", "name": "id", "type": "INT", "pk": True, "fk": False},
            {"table": "orders", "name": "id", "type": "INT", "pk": True, "fk": False},
            {"table": "orders", "name": "status_code", "type": "VARCHAR(16)", "pk": False, "fk": False},
        ],
    )
    assert _pairs(schema) == []


def test_key_and_ref_suffixes():
    """_key/_ref 后缀生效。"""
    schema = _schema(
        tables=[
            {"name": "users", "column_count": 2},
            {"name": "sessions", "column_count": 2},
            {"name": "payments", "column_count": 2},
        ],
        columns=[
            {"table": "users", "name": "id", "type": "INT", "pk": True, "fk": False},
            {"table": "sessions", "name": "id", "type": "INT", "pk": True, "fk": False},
            {"table": "sessions", "name": "user_key", "type": "INT", "pk": False, "fk": False},
            {"table": "payments", "name": "id", "type": "INT", "pk": True, "fk": False},
            {"table": "payments", "name": "user_ref", "type": "INT", "pk": False, "fk": False},
        ],
    )
    pairs = _pairs(schema)
    assert ("sessions", "user_key", "users", "id") in pairs
    assert ("payments", "user_ref", "users", "id") in pairs


def test_target_column_missing_skipped():
    """目标表无主键/id/code 列 → 跳过候选。"""
    schema = _schema(
        tables=[{"name": "customers", "column_count": 1}, {"name": "orders", "column_count": 2}],
        columns=[
            {"table": "customers", "name": "email", "type": "VARCHAR(64)", "pk": False, "fk": False},
            {"table": "orders", "name": "id", "type": "INT", "pk": True, "fk": False},
            {"table": "orders", "name": "customer_id", "type": "INT", "pk": False, "fk": False},
        ],
    )
    assert _pairs(schema) == []


def test_self_loop_skipped():
    """同表引用（parent_id 无 parent 表）不生成；目标即本表跳过。"""
    schema = _schema(
        tables=[{"name": "categories", "column_count": 2}],
        columns=[
            {"table": "categories", "name": "id", "type": "INT", "pk": True, "fk": False},
            {"table": "categories", "name": "parent_id", "type": "INT", "pk": False, "fk": False},
        ],
    )
    assert _pairs(schema) == []