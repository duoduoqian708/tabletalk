"""敏感名单：屏蔽命中表/列，使其不进 schema 上下文与知识库构建。

两种条目可混排：字符串 glob（旧名单）+ 精确名结构 {table, columns[]}（段8）。
"""
from __future__ import annotations

from app.core.sensitive import filter_sensitive


def test_filter_sensitive_removes_matched_tables():
    schema = {
        "tables": [
            {"name": "orders", "columns": [{"name": "id"}, {"name": "payroll_band"}]},
            {"name": "payroll_2024", "columns": [{"name": "id"}]},
            {"name": "products", "columns": [{"name": "id"}, {"name": "name"}]},
        ]
    }
    out = filter_sensitive(schema, ["payroll_*"])
    names = [t["name"] for t in out["tables"]]
    assert "payroll_2024" not in names                      # 表级 glob 命中剔除
    orders = next(t for t in out["tables"] if t["name"] == "orders")
    assert "payroll_band" not in [c["name"] for c in orders["columns"]]  # 列级剔除
    assert "products" in names                              # 未命中保留


def test_filter_sensitive_empty_patterns_is_noop():
    schema = {"tables": [{"name": "orders", "columns": [{"name": "id"}]}]}
    assert filter_sensitive(schema, []) == schema


def _schema() -> dict:
    return {
        "tables": [
            {"name": "orders", "kind": "table", "column_count": 4},
            {"name": "secret_log", "kind": "table", "column_count": 2},
            {"name": "customers", "kind": "table", "column_count": 4},
        ],
        "columns": [
            {"table": "orders", "name": "id", "type": "int"},
            {"table": "orders", "name": "customer_email", "type": "text"},
            {"table": "orders", "name": "customer_phone", "type": "text"},
            {"table": "secret_log", "name": "id", "type": "int"},
            {"table": "customers", "name": "id", "type": "int"},
            {"table": "customers", "name": "phone", "type": "text"},
        ],
        "foreign_keys": [
            {"table": "orders", "column": "customer_id", "ref_table": "secret_log", "ref_column": "id"},
            {"table": "orders", "column": "customer_id", "ref_table": "customers", "ref_column": "phone"},
        ],
    }


def test_exact_table_without_columns_excludes_whole_table():
    """dict{table, columns:[]} → 整表排除（含顶层列 + 引用它的 FK）。"""
    out = filter_sensitive(_schema(), [{"table": "secret_log", "columns": []}])
    assert [t["name"] for t in out["tables"]] == ["orders", "customers"]
    assert "secret_log" not in {c["table"] for c in out["columns"]}
    # 引用 secret_log 的 FK 移除；customers 未受影响 → 其 FK 保留
    assert len(out["foreign_keys"]) == 1
    assert out["foreign_keys"][0]["ref_table"] == "customers"


def test_exact_columns_table_scoped():
    """dict{table, columns非空} → 仅剔除该表内对应列，其他表同名列不受影响。"""
    out = filter_sensitive(_schema(), [{"table": "customers", "columns": ["phone"]}])
    # customers 表保留，phone 列仅在 customers 内被剔除
    assert "customers" in [t["name"] for t in out["tables"]]
    cols = {(c["table"], c["name"]) for c in out["columns"]}
    assert ("customers", "phone") not in cols
    assert ("orders", "customer_phone") in cols  # 其他表不受连坐
    # customers.phone 被剔除 → 引用它的 FK 移除；secret_log 那条保留
    assert len(out["foreign_keys"]) == 1
    assert out["foreign_keys"][0]["ref_table"] == "secret_log"


def test_exact_table_key_only_no_columns_key():
    """dict 只给 table（无 columns 键）→ 整表排除。"""
    out = filter_sensitive(_schema(), [{"table": "secret_log"}])
    assert "secret_log" not in [t["name"] for t in out["tables"]]


def test_exact_names_case_insensitive():
    """精确名结构大小写归一（与 glob 分支一致）：手输表名/列名大小写不符仍生效。"""
    out = filter_sensitive(_schema(), [{"table": "Secret_Log", "columns": []}])
    assert "secret_log" not in [t["name"] for t in out["tables"]]
    out2 = filter_sensitive(_schema(), [{"table": "CUSTOMERS", "columns": ["PHONE"]}])
    cols = {(c["table"], c["name"]) for c in out2["columns"]}
    assert ("customers", "phone") not in cols and ("customers", "id") in cols


def test_mixed_glob_and_exact():
    """glob 字符串 + 精确名结构可混排；各自语义互不影响。"""
    out = filter_sensitive(
        _schema(),
        ["secret_*", {"table": "customers", "columns": ["phone"]}],
    )
    # secret_log 被 glob 剔除；orders 与 customers 保留（glob 与精确名互不影响）
    assert [t["name"] for t in out["tables"]] == ["orders", "customers"]
    cols = {(c["table"], c["name"]) for c in out["columns"]}
    assert ("customers", "id") in cols and ("customers", "phone") not in cols
