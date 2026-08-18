"""敏感名单：屏蔽命中表/列，使其不进 schema 上下文与知识库构建。"""

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
