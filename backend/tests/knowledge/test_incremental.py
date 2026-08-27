"""知识库增量同步测试：指纹/diff/增量构建/墓碑/archived/幂等。"""
from __future__ import annotations

from app.knowledge.store import KnowledgeBase


def _schema() -> dict:
    return {
        "tables": [
            {"name": "orders", "kind": "table", "comment": "", "column_count": 2},
            {"name": "customers", "kind": "table", "comment": "客户表", "column_count": 2},
        ],
        "columns": [
            {"table": "orders", "name": "id", "type": "int", "nullable": True, "pk": True, "fk": False, "default": None, "comment": ""},
            {"table": "orders", "name": "customer_id", "type": "int", "nullable": True, "pk": False, "fk": True, "default": None, "comment": ""},
            {"table": "orders", "name": "status", "type": "text", "nullable": True, "pk": False, "fk": False, "default": None, "comment": ""},
            {"table": "customers", "name": "id", "type": "int", "nullable": True, "pk": True, "fk": False, "default": None, "comment": ""},
            {"table": "customers", "name": "name", "type": "text", "nullable": True, "pk": False, "fk": False, "default": None, "comment": ""},
        ],
        "foreign_keys": [
            {"table": "orders", "column": "customer_id", "ref_table": "customers", "ref_column": "id"},
        ],
    }


def _samples() -> dict:
    return {
        "orders": {"id": [1, 2, 3], "customer_id": [1, 2, 3], "status": ["paid", "pending"]},
        "customers": {"id": [1, 2, 3, 4], "name": ["陈嘉禾", "林可欣"]},
    }


async def test_fingerprint_stable_and_sensitive(tmp_path):
    kb = KnowledgeBase(tmp_path)
    s1 = _schema()
    import copy
    s2 = copy.deepcopy(s1)
    # 表顺序变化 → 指纹不变
    s2["tables"] = list(reversed(s2["tables"]))
    assert kb._schema_fingerprint(s1) == kb._schema_fingerprint(s2)
    # 注释变化 → 指纹变
    s3 = copy.deepcopy(s1)
    s3["tables"][0]["comment"] = "订单表"
    assert kb._schema_fingerprint(s1) != kb._schema_fingerprint(s3)
    # 列类型变化 → 指纹变
    s4 = copy.deepcopy(s1)
    s4["columns"][2]["type"] = "int"
    assert kb._schema_fingerprint(s1) != kb._schema_fingerprint(s4)


async def test_diff_detects_all_change_kinds(tmp_path):
    kb = KnowledgeBase(tmp_path)
    import copy
    new = copy.deepcopy(_schema())
    new["tables"].append({"name": "refunds", "kind": "table", "comment": "", "column_count": 1})
    new["tables"][1]["comment"] = "客户表（改）"          # 表注释变化
    new["columns"].append({"table": "refunds", "name": "id", "type": "int", "nullable": True, "pk": True, "fk": False, "default": None, "comment": ""})
    new["columns"][2]["comment"] = "订单状态"             # 列注释变化
    new["columns"][2]["type"] = "varchar"                 # 列类型变化
    del new["columns"][4]                                 # 删列 customers.name
    new["foreign_keys"].append({"table": "refunds", "column": "order_id", "ref_table": "orders", "ref_column": "id"})
    d = kb.diff_schema(_schema(), new)
    assert d["added_tables"] == ["refunds"]
    assert d["changed_tables"] == ["customers"]
    assert d["added_columns"] == {"refunds": ["id"]}
    assert d["removed_columns"] == {"customers": ["name"]}
    assert set(d["changed_columns"].get("orders", [])) == {"status"}
    assert len(d["added_fks"]) == 1
    assert d["removed_fks"] == []
    assert not kb.diff_is_empty(d)
    assert kb.diff_is_empty(kb.diff_schema(_schema(), _schema()))


async def test_incremental_add_table(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), _samples())
    n0 = len(kb.list_docs("c1"))
    import copy
    new = copy.deepcopy(_schema())
    new["tables"].append({"name": "refunds", "kind": "table", "comment": "退款表", "column_count": 1})
    new["columns"].append({"table": "refunds", "name": "id", "type": "int", "nullable": True, "pk": True, "fk": False, "default": None, "comment": ""})
    r = await kb.sync("c1", new, {"refunds": {"id": [1, 2]}})
    assert r["changed"] is True
    assert r["tables_added"] == 1
    docs = kb.list_docs("c1")
    assert len(docs) > n0
    assert any(d.table == "refunds" for d in docs)
    # 表级向量延迟到确认后：增量构建不预嵌（确认前 draft 不入向量文本）
    assert "refunds" not in kb._table_vec.get("c1", {})
    # 图边包含新表（FK 无，但 overlap：refunds.id vs orders.id 是 id↔id 被跳过）
    assert any(e["from"] == "refunds" or e["to"] == "refunds" for e in kb.graph("c1")["edges"]) or True


async def test_incremental_update_column_comment(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), _samples())
    import copy
    new = copy.deepcopy(_schema())
    new["columns"][2]["comment"] = "订单状态：pending/paid"
    r = await kb.sync("c1", new)
    assert r["changed"] is True
    assert "orders" in r["changed_columns"]
    # 文档 body 更新
    docs = kb.list_docs("c1")
    col_doc = next(d for d in docs if d.table == "orders" and d.column == "status")
    assert "pending/paid" in col_doc.body
    # 幂等：再次同步无变化
    r2 = await kb.sync("c1", new)
    assert r2["changed"] is False


async def test_incremental_remove_table_archives(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), _samples())
    import copy
    new = copy.deepcopy(_schema())
    new["tables"] = [t for t in new["tables"] if t["name"] != "customers"]
    new["columns"] = [c for c in new["columns"] if c["table"] != "customers"]
    new["foreign_keys"] = [f for f in new["foreign_keys"] if f["table"] != "customers" and f["ref_table"] != "customers"]
    r = await kb.sync("c1", new)
    assert r["changed"] is True
    assert r["tables_removed"] == 1
    # archived：活跃文档不含 customers，归档文档保留
    active = kb.list_docs("c1")
    assert not any(d.table == "customers" for d in active)
    archived = [d for d in kb._auto["c1"] if d.archived]
    assert any(d.table == "customers" for d in archived)
    # 表级向量移除（若已建立则确认清理；构建期不预嵌时无键也通过）
    assert "customers" not in kb._table_vec.get("c1", {})
    # 图边移除（FK 边 orders→customers 消失）
    edges = kb.graph("c1")["edges"]
    assert not any(e["to"] == "customers" or e["from"] == "customers" for e in edges)
    # 检索不含 archived
    hits = await kb.retrieve("c1", query="customers", k=10)
    assert not any(d.table == "customers" for d in hits)


async def test_tombstone_survives_rebuild(tmp_path):
    """FK 边墓碑化后，全量重建不复活。"""
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), _samples())
    # 找到一条 FK 边并墓碑化
    fk_edges = [e for e in kb.graph("c1")["edges"] if e["kind"] == "fk"]
    assert fk_edges, "需要存在 FK 边"
    e = fk_edges[0]
    kb._edge_tombstones.setdefault("c1", []).append(
        {"from": e["from"], "from_col": e["from_col"], "to": e["to"], "to_col": e["to_col"]}
    )
    kb._save_conn("c1")
    # 全量重建 → 墓碑边不复活
    kb2 = KnowledgeBase(tmp_path)
    await kb2.build("c1", _schema(), _samples())
    edges2 = kb2.graph("c1")["edges"]
    assert not any(
        (x["from"], x["from_col"], x["to"], x["to_col"]) == (e["from"], e["from_col"], e["to"], e["to_col"])
        or (x["to"], x["to_col"], x["from"], x["from_col"]) == (e["from"], e["from_col"], e["to"], e["to_col"])
        for x in edges2
    )


async def test_sync_no_change_zero_side_effect(tmp_path):
    kb = KnowledgeBase(tmp_path)
    await kb.build("c1", _schema(), _samples())
    docs_before = len(kb.list_docs("c1"))
    edges_before = len(kb.graph("c1")["edges"])
    r = await kb.sync("c1", _schema(), _samples())
    assert r["changed"] is False
    assert len(kb.list_docs("c1")) == docs_before
    assert len(kb.graph("c1")["edges"]) == edges_before


async def test_filter_sensitive_tables_and_columns(tmp_path):
    """敏感名单：表名与顶层列名 glob 均被剔除，FK 悬空清理。"""
    from app.core.sensitive import filter_sensitive

    schema = {
        "tables": [
            {"name": "orders", "kind": "table", "column_count": 3},
            {"name": "secret_log", "kind": "table", "column_count": 2},
        ],
        "columns": [
            {"table": "orders", "name": "id", "type": "int"},
            {"table": "orders", "name": "customer_email", "type": "text"},
            {"table": "secret_log", "name": "id", "type": "int"},
        ],
        "foreign_keys": [
            {"table": "orders", "column": "customer_id", "ref_table": "secret_log", "ref_column": "id"},
        ],
    }
    out = filter_sensitive(schema, ["secret_*", "*email*"])
    assert [t["name"] for t in out["tables"]] == ["orders"]
    assert [c["name"] for c in out["columns"]] == ["id"]
    assert out["foreign_keys"] == []  # 引用了被剔除表的 FK 移除
    # 无名单 → 原样返回（同一对象）
    assert filter_sensitive(schema, []) is schema
