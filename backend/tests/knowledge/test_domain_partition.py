"""段6：领域划分 v2 测试——画像 / 解析 / 校验兜底 / 一轮划分+一轮自检 / 不再写 desc_drafts。"""
from __future__ import annotations

import json

from app.knowledge.annotator import (
    _mock_domains,
    _normalize_domains,
    _parse_domain_payload,
    _single_table_ddl_portrait,
)


def _dom_schema() -> dict:
    return {
        "tables": [
            {"name": "orders", "kind": "table", "comment": "订单主表", "column_count": 3},
            {"name": "order_items", "kind": "table", "comment": "", "column_count": 2},
            {"name": "customers", "kind": "table", "comment": "", "column_count": 2},
            {"name": "products", "kind": "table", "comment": "", "column_count": 2},
            {"name": "users", "kind": "table", "comment": "", "column_count": 2},
            {"name": "audit_log", "kind": "table", "comment": "", "column_count": 3},
        ],
        "columns": [
            {"table": "orders", "name": "id", "pk": True, "comment": "订单号"},
            {"table": "orders", "name": "customer_id", "pk": False, "comment": ""},
            {"table": "orders", "name": "status", "pk": False, "comment": ""},
            {"table": "order_items", "name": "order_id", "pk": False, "comment": ""},
            {"table": "order_items", "name": "product_id", "pk": False, "comment": ""},
            {"table": "customers", "name": "id", "pk": True, "comment": ""},
            {"table": "products", "name": "id", "pk": True, "comment": ""},
            {"table": "users", "name": "id", "pk": True, "comment": ""},
            {"table": "audit_log", "name": "created_at", "pk": False, "comment": ""},
        ],
        "foreign_keys": [],
    }


# ---------- 画像（精简 DDL：描述回填 + 取值示例 + FK 内联） ----------


def test_portrait_ddl_structure_noise_filtered_uses_desc():
    """DDL 骨架保留、噪音列滤除、表描述头来自 table_desc（库注释优先）。"""
    p = _single_table_ddl_portrait(_dom_schema(), "orders", "订单主表", {}, None)
    assert "CREATE TABLE orders (" in p
    assert "-- orders：订单主表" in p
    assert "customer_id" in p            # 业务列保留
    lines = [ln for ln in p.splitlines() if ln.startswith("  audit_log") or "created_at" in ln]
    assert not lines                     # 噪音列（时间戳）不进画像


def test_portrait_desc_and_values_and_fk():
    """列描述回填 + 有采样才给取值示例 + FK 内联 [FK->]。"""
    sch = dict(_dom_schema())
    sch["foreign_keys"] = [
        {"table": "orders", "column": "customer_id", "ref_table": "customers", "ref_column": "id"}
    ]
    # 有采样：非高基数列（status TEXT）给取值集
    p = _single_table_ddl_portrait(sch, "orders", "订单主表",
                                   {"customer_id": "下单客户", "status": "订单状态"},
                                   {"status": ["paid", "pending", "shipped"], "id": [1, 2]})
    assert "[FK->customers.id]" in p     # FK 内联
    assert "描述：下单客户" in p          # 列描述回填
    assert "取值示例：paid，pending，shipped" in p  # 有采样 → 低基数取值集
    # 无采样：不给取值示例
    p0 = _single_table_ddl_portrait(sch, "orders", "订单主表", {}, None)
    assert "取值示例" not in p0
    assert "[FK->customers.id]" in p0    # FK 有就带（与采样无关）


# ---------- 解析 ----------


def test_parse_domain_payload_array_and_unchanged():
    ok, doms = _parse_domain_payload(
        '[{"name":"订单域","description":"订单一类","tables":["orders"],"reason":"核心"}]'
    )
    assert ok is False and doms[0]["name"] == "订单域" and doms[0]["tables"] == ["orders"]
    ok, doms = _parse_domain_payload('{"unchanged": true}')
    assert ok is True and doms == []
    # code fence + 垃圾
    assert _parse_domain_payload('```json\n[{"name":"x","tables":[]}]\n```')[1][0]["name"] == "x"
    assert _parse_domain_payload("抱歉") == (False, [])


# ---------- 校验 + 兜底 ----------


def test_normalize_domains_drops_unknown_and_orphan_fill():
    names = ["orders", "order_items", "customers"]
    doms = [{"name": "订单域", "tables": ["orders", "not_a_table"], "reason": ""}]
    out = _normalize_domains(doms, names)
    assert len(out) == 1
    assert sorted(out[0]["tables"]) == ["customers", "order_items", "orders"]  # 未知表剔除 + 孤表并入


def test_normalize_domains_empty_fallback_single_domain():
    out = _normalize_domains([], ["a", "b"])
    assert out and out[0]["name"] == "业务"
    assert "a" in out[0]["tables"] and "b" in out[0]["tables"]


def test_normalize_domains_count_cap():
    names = ["a", "b", "c"]
    doms = [{"name": f"d{i}", "tables": [n]} for i, n in enumerate(names)]
    # 3 表 3 域 → 合法（域数≤表数）
    assert len(_normalize_domains(doms, names)) == 3
    # 加一个多余空域 → 仍 3（成员为空者先被剔除）
    doms_plus = doms + [{"name": "空域", "tables": []}]
    assert len(_normalize_domains(doms_plus, names)) == 3


def test_mock_domains_groups_multi_table():
    doms = _mock_domains(_dom_schema())
    names = [d["name"] for d in doms]
    assert "订单" in names and "客户" in names
    order = next(d for d in doms if d["name"] == "订单")
    assert "orders" in order["tables"] and "order_items" in order["tables"]


# ---------- 行为：一轮划分 + 自检（LLM 假 provider） ----------


class _DomainProv:
    """假 provider：第一轮划分，第二轮按 mode 返回。"""

    def __init__(self, selfcheck: str = "unchanged") -> None:
        self.selfcheck = selfcheck
        self.rounds: list[tuple[str, dict]] = []

    async def chat(self, messages, tools=None, ctx=None):
        prompt = messages[0]["content"]
        self.rounds.append((prompt, ctx))
        if "【初版划分】" in prompt:  # 第二轮自检
            if self.selfcheck == "unchanged":
                return type("R", (), {"content": '{"unchanged": true}'})()
            return type("R", (), {"content": json.dumps([
                {"name": "订单域", "description": "订单一类", "tables": ["orders", "order_items"], "reason": "合并明细"},
                {"name": "客户域", "description": "客户", "tables": ["customers"], "reason": ""},
                {"name": "商品域", "description": "商品型录", "tables": ["products"], "reason": ""},
            ])})()
        return type("R", (), {"content": json.dumps([
            {"name": "订单域", "description": "订单一类", "tables": ["orders", "order_items"], "reason": "核心"},
            {"name": "客户域", "description": "客户", "tables": ["customers"], "reason": ""},
        ])})()


async def _setup_kb(st, conn: str, schema: dict) -> None:
    await st.knowledge.build(conn, schema, enable_ai_annotation=False)


async def test_annotate_domain_partition_plus_selfcheck_no_desc_drafts(app_state, monkeypatch):
    """一轮划分 + 自检（unchanged 保留初版）；不写 desc_drafts；每表至少一个领域。"""
    from app.knowledge import annotator as ann

    st = app_state
    conn = "c-dom"
    schema = _dom_schema()
    await _setup_kb(st, conn, schema)
    prov = _DomainProv("unchanged")
    monkeypatch.setattr(ann.gw, "is_effective_mock", lambda cfg: False)
    monkeypatch.setattr(ann.gw, "build_provider", lambda cfg: prov)

    res = await ann.annotate_domain(st, conn, schema=schema)
    assert res["tables"] == 6 and res["domains"] >= 1
    # 两次调用：划分 + 自检，ctx 状态区分
    assert len(prov.rounds) == 2
    assert prov.rounds[0][1]["status"] == "egress-tags"
    assert prov.rounds[1][1]["status"] == "egress-tags-selfcheck"
    # 不再写 desc_drafts：无任何表级草案（本测试未跑阶段一，表壳无需描述）
    tabs = st.knowledge._tables[conn]
    assert all(tk.status != "draft" for tk in tabs.values()), "领域阶段不得再写表级描述草案"
    # 无孤表：每张表至少属于一个域
    tables_map = st.knowledge.tags(conn)["tables"]
    assert set(tables_map) == {t["name"] for t in schema["tables"]}


async def test_annotate_domain_selfcheck_accepts_revision(app_state, monkeypatch):
    """自检返回修正版 → 采纳（覆盖初版划分带来的标签差异）。"""
    from app.knowledge import annotator as ann

    st = app_state
    conn = "c-dom2"
    schema = _dom_schema()
    await _setup_kb(st, conn, schema)
    prov = _DomainProv("revised")  # 修正版新增「商品域」，并移动订单明细归属
    monkeypatch.setattr(ann.gw, "is_effective_mock", lambda cfg: False)
    monkeypatch.setattr(ann.gw, "build_provider", lambda cfg: prov)

    res = await ann.annotate_domain(st, conn, schema=schema)
    assert res["domains"] >= 3  # 修正版 3 域（初版 2 域）
    tags_map = st.knowledge.tags(conn)["tables"]
    assert "products" in tags_map and "商品域" in tags_map["products"]


async def test_annotate_domain_selfcheck_toggle_off_skips_round2(app_state, monkeypatch):
    """kb_build_self_check=False → 只一轮划分（不调自检）。"""
    from app.knowledge import annotator as ann

    st = app_state
    conn = "c-dom3"
    schema = _dom_schema()
    await _setup_kb(st, conn, schema)
    st.runtime.update({"kb_build_self_check": False})
    prov = _DomainProv("unchanged")
    monkeypatch.setattr(ann.gw, "is_effective_mock", lambda cfg: False)
    monkeypatch.setattr(ann.gw, "build_provider", lambda cfg: prov)

    res = await ann.annotate_domain(st, conn, schema=schema)
    assert len(prov.rounds) == 1
    assert res["domains"] >= 1

async def test_annotate_domain_selfcheck_param_overrides_runtime_on(app_state, monkeypatch):
    """显式 self_check=False 覆盖运行时默认开：即使运行时为 True 也不调自检。"""
    from app.knowledge import annotator as ann

    st = app_state
    conn = "c-dom-ovr"
    schema = _dom_schema()
    await _setup_kb(st, conn, schema)
    # 运行时默认开（True）；构建期显式关 → 以显式为准
    st.runtime.update({"kb_build_self_check": True})
    prov = _DomainProv("unchanged")
    monkeypatch.setattr(ann.gw, "is_effective_mock", lambda cfg: False)
    monkeypatch.setattr(ann.gw, "build_provider", lambda cfg: prov)

    res = await ann.annotate_domain(st, conn, schema=schema, self_check=False)
    assert len(prov.rounds) == 1
    assert res["domains"] >= 1
