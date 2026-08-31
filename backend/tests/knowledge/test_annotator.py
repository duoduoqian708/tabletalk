"""AI 自动注释器测试：prompt 构造 / JSON 解析 / mock 确定性注释。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from app.knowledge.annotator import (
    _generate_candidate_pairs,
    _kb_reason_provider_cfg,
    _mock_comments,
    _parse_graph_edges,
    _parse_items,
    annotate_knowledge,
)


def _schema() -> dict:
    return {
        "tables": [
            {"name": "orders", "kind": "table", "comment": "订单表", "column_count": 1},
            {"name": "customers", "kind": "table", "comment": "", "column_count": 1},
        ],
        "columns": [
            {"table": "orders", "name": "id", "type": "int", "pk": True, "fk": False, "comment": ""},
            {"table": "customers", "name": "name", "type": "text", "pk": False, "fk": False, "comment": "客户姓名"},
        ],
    }


def test_mock_comments_skips_existing():
    items = _mock_comments(_schema(), {})
    # orders 已有表注释 → 不生成表级；customers 无 → 生成
    assert not any(i["table"] == "orders" and i["column"] is None for i in items)
    assert any(i["table"] == "customers" and i["column"] is None for i in items)
    # customers.name 已有注释 → 不生成
    assert not any(i["table"] == "customers" and i["column"] == "name" for i in items)
    # orders.id 无注释 → 生成
    assert any(i["table"] == "orders" and i["column"] == "id" for i in items)


def test_mock_comments_can_include_samples():
    samples = {"orders": {"id": [1, 2, 3]}}
    items = _mock_comments(_schema(), samples)
    id_item = next(i for i in items if i["table"] == "orders" and i["column"] == "id")
    assert "示例取值" in id_item["comment"]


def _reason_rt(timeout=60, cap=None, mid="", effort="low"):
    return SimpleNamespace(
        provider_config=lambda: {"provider": "cloud", "model": "m", "timeout": timeout},
        default_ai_model=mid,
        ai_models=[SimpleNamespace(id=mid, capabilities=cap)] if mid else [],
        kb_build_reasoning_effort=effort,
    )


def test_kb_reason_provider_cfg_extends_timeout_and_shallow_default():
    cfg = _kb_reason_provider_cfg(
        _reason_rt(timeout=60, cap={"reasoning": True, "reasoning_effort": "high"}, mid="m")
    )
    assert cfg["timeout"] == 300.0, "推理调用读超时应放宽到 300s（不被 120s 掐断）"
    assert cfg["reasoning"] == "low", "默认浅推理（不搞深度）→ 支持档位模型直接传 low"


def test_kb_reason_provider_cfg_effort_knob_overrides():
    """kb_build_reasoning_effort=medium/high → 跟随档位；off → 显式关思考。"""
    rt = _reason_rt(timeout=60, cap={"reasoning": True, "reasoning_effort": "high"}, mid="m")
    assert _kb_reason_provider_cfg(SimpleNamespace(**{**vars(rt), "kb_build_reasoning_effort": "high"}))["reasoning"] == "high"
    assert _kb_reason_provider_cfg(SimpleNamespace(**{**vars(rt), "kb_build_reasoning_effort": "medium"}))["reasoning"] == "medium"
    off = _kb_reason_provider_cfg(SimpleNamespace(**{**vars(rt), "kb_build_reasoning_effort": "off"}))
    assert off["reasoning"] == "off", "档位 off → 阶段3 也走普通生成"


def test_kb_reason_provider_cfg_thinking_only_gets_budget_shallow():
    """只支持 thinking 的模型（如 DeepSeek）：档位 low → thinking + budget_tokens=1024 压浅推理。"""
    cfg = _kb_reason_provider_cfg(
        _reason_rt(timeout=60, cap={"reasoning": True}, mid="m")  # 无 reasoning_effort
    )
    assert cfg["reasoning"] == "thinking", "thinking-only 模型 → 只启思考"
    assert cfg["thinking_budget"] == 1024, "浅推理 → budget_tokens 1024 压思考链"

    rt_hi = _reason_rt(timeout=60, cap={"reasoning": True}, mid="m", effort="high")
    hi = _kb_reason_provider_cfg(rt_hi)
    assert hi["thinking_budget"] == 8192, "高档 → 更大预算"


def test_kb_reason_provider_cfg_respects_user_longer_timeout():
    cfg = _kb_reason_provider_cfg(
        _reason_rt(timeout=600, cap={"reasoning": True}, mid="m")
    )
    assert cfg["timeout"] == 600.0, "用户已配更长超时不反压"


def test_kb_reason_provider_cfg_no_capability_still_extends_timeout():
    cfg = _kb_reason_provider_cfg(_reason_rt(timeout=120))
    assert cfg["timeout"] == 300.0
    assert "reasoning" not in cfg, "无能力探测 → reasoning 保持原样"


def test_kb_reason_provider_cfg_off_for_stage2():
    cfg = _kb_reason_provider_cfg(
        _reason_rt(timeout=60, cap={"reasoning": True, "reasoning_effort": "high"}, mid="m"),
        reasoning=False,
    )
    assert cfg["reasoning"] == "off", "阶段2（领域划分）显式关思考 → 网关不发 thinking 参数"
    assert cfg["timeout"] == 300.0, "超时放宽仍生效"


def test_parse_items_plain_json():
    text = '[{"table":"orders","column":"id","comment":"主键"},{"table":"orders","comment":"订单主表"}]'
    items = _parse_items(text)
    assert len(items) == 2
    assert items[0]["column"] == "id"
    assert items[1]["column"] is None


def test_parse_items_code_fence():
    text = '```json\n[{"table":"orders","column":"status","comment":"状态"}]```'
    assert _parse_items(text) == [{"table": "orders", "column": "status", "comment": "状态"}]


def test_parse_items_garbage():
    assert _parse_items("抱歉，我无法") == []
    assert _parse_items("") == []


# ---------- 段4：两套模板（有采样 / 无采样）不分家 ----------


def _ann_schema() -> dict:
    return {
        "tables": [{"name": "orders", "kind": "table", "comment": "", "column_count": 2}],
        "columns": [
            {"table": "orders", "name": "id", "type": "int", "pk": True, "fk": False, "comment": ""},
            {"table": "orders", "name": "status", "type": "varchar(16)", "pk": False, "fk": False, "comment": ""},
        ],
    }


def test_two_templates_prompt_contract():
    from app.knowledge.annotator import (
        _annotation_prompt_sampled,
        _annotation_prompt_unsampled,
    )

    ddl = "CREATE TABLE orders (id int, status varchar(16))"
    sampled = _annotation_prompt_sampled(ddl, "【样本取值】\n- status: ['P','S']")
    unsampled = _annotation_prompt_unsampled(ddl)
    # 有采样版：样本段 + values 指令 + 含 values 的 JSON 契约
    assert "【样本取值" in sampled and "取值" in sampled
    assert '"values"' in sampled
    # 无采样版：不提样本、不提 values（纯结构）
    assert "样本" not in unsampled
    assert "values" not in unsampled
    assert '"values"' not in unsampled


class _RecProv:
    """非 mock 探针：记录 prompt，返回（硬凑了 values 的）确定性注释。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def chat(self, messages, tools=None, ctx=None):
        self.prompts.append(messages[0]["content"])
        return type("R", (), {"content": (
            '[{"table":"orders","column":"status","comment":"订单状态",'
            '"values":"P=待付款；S=已发货"}]')})()


async def test_annotate_table_sampled_template(app_state, monkeypatch):
    from app.knowledge import annotator as ann

    prov = _RecProv()
    monkeypatch.setattr(ann.gw, "is_effective_mock", lambda cfg: False)
    monkeypatch.setattr(ann.gw, "build_provider", lambda cfg: prov)
    items = await ann.annotate_table(
        app_state, "c", "orders", "CREATE TABLE orders (...)", _ann_schema(),
        samples={"orders": {"status": ["P", "S"], "id": [3]}},
    )
    p = prov.prompts[0]
    assert "【样本取值" in p and "低基数离散取值" in p
    assert '"values"' in p
    assert items[0]["values"] == "P=待付款；S=已发货"
    assert items[0]["example"]  # 后端从样本提取 example


async def test_annotate_table_unsampled_template_drops_values(app_state, monkeypatch):
    """无采样版：prompt 不提 values；且 LLM 即便硬凑 values，落库前也被丢弃。"""
    from app.knowledge import annotator as ann

    prov = _RecProv()  # 探针固定返回带 values 的响应 → 检验硬闸
    monkeypatch.setattr(ann.gw, "is_effective_mock", lambda cfg: False)
    monkeypatch.setattr(ann.gw, "build_provider", lambda cfg: prov)
    items = await ann.annotate_table(
        app_state, "c", "orders", "CREATE TABLE orders (...)", _ann_schema(),
        samples=None,
    )
    p = prov.prompts[0]
    assert "样本" not in p and "values" not in p
    assert items[0]["comment"] == "订单状态"      # 注释保留
    assert items[0].get("values") is None          # values 被硬闸丢弃
    assert items[0].get("example") is None         # example 无样本不产


# ---------- annotate_knowledge 独立路径：出网不变量 ----------


async def test_annotate_knowledge_truncates_samples_before_send(app_state):
    """独立 annotate 路径：授权样本发送前统一值级截断（与枚举 core 同款不变量）。"""
    st = app_state
    long_val = "超长业务取值-" + "很长的说明" * 30  # 远超 60 字符
    schema = {
        "tables": [{"name": "orders", "kind": "table", "comment": "", "column_count": 1}],
        "columns": [
            {"table": "orders", "name": "status", "type": "varchar(16)", "pk": False, "fk": False, "comment": ""},
        ],
    }
    # v2：草案落 ColumnInfo，需先有表壳（离线构建即建壳；DDL 实时获取失败自动回退快照合成）
    await st.knowledge.build("c-anno", schema, enable_ai_annotation=False)
    res = await annotate_knowledge(
        st, "c-anno", include_samples=True, schema=schema,
        samples={"orders": {"status": [long_val]}},
    )
    assert res["added"] > 0
    body = st.knowledge._tables["c-anno"]["orders"].columns["status"].proposed_comment
    assert long_val[:60] in body   # 截断值进入草案（= 发送内容）
    assert long_val not in body    # 原始长句不出网/不入库


# ---------- T2：边 v2 解析与候选（四元组 + cardinality + 多侧校验） ----------


def _graph_schema() -> dict:
    return {
        "tables": [{"name": "orders"}, {"name": "customers"}],
        "columns": [
            {"table": "orders", "name": "id", "pk": True},
            {"table": "orders", "name": "customer_id", "pk": False},
            {"table": "customers", "name": "id", "pk": True},
        ],
    }


def test_parse_graph_edges_four_tuple_and_cardinality():
    """四元组 + cardinality 齐备才收；缺字段/缺基数丢弃。"""
    ok = _parse_graph_edges(
        '[{"from_table":"orders","from_col":"customer_id","to_table":"customers","to_col":"id",'
        '"cardinality":"n:1","reason":"订单归属客户"}]', _graph_schema())
    assert len(ok) == 1
    assert ok[0]["from_col"] == "customer_id" and ok[0]["cardinality"] == "n:1"
    # 缺 from_col → 丢弃
    assert _parse_graph_edges(
        '[{"from_table":"orders","to_table":"customers","cardinality":"n:1"}]', _graph_schema()) == []
    # 缺 cardinality → 丢弃
    assert _parse_graph_edges(
        '[{"from_table":"orders","from_col":"customer_id","to_table":"customers","to_col":"id"}]',
        _graph_schema()) == []
    # 非法基数 → 丢弃
    assert _parse_graph_edges(
        '[{"from_table":"orders","from_col":"customer_id","to_table":"customers","to_col":"id",'
        '"cardinality":"m:n"}]', _graph_schema()) == []


def test_parse_graph_edges_many_side_validation():
    """多侧校验：from_col 为 from_table 主键却声明 n:1 → 矛盾丢弃；1:1 保留。"""
    schema = _graph_schema()
    bad = _parse_graph_edges(
        '[{"from_table":"orders","from_col":"id","to_table":"customers","to_col":"id",'
        '"cardinality":"n:1"}]', schema)
    assert bad == []
    ok = _parse_graph_edges(
        '[{"from_table":"orders","from_col":"id","to_table":"customers","to_col":"id",'
        '"cardinality":"1:1"}]', schema)
    assert len(ok) == 1 and ok[0]["cardinality"] == "1:1"
    # 字段不存在 → 丢弃；自环 → 丢弃
    assert _parse_graph_edges(
        '[{"from_table":"orders","from_col":"nope","to_table":"customers","to_col":"id",'
        '"cardinality":"n:1"}]', schema) == []
    assert _parse_graph_edges(
        '[{"from_table":"orders","from_col":"id","to_table":"orders","to_col":"id",'
        '"cardinality":"1:1"}]', schema) == []


def test_generate_candidate_pairs_cardinality():
    """候选对：from=持有引用列的表（多侧）；列兼主键 → 1:1，否则 n:1。"""
    schema = {
        "tables": [{"name": "order"}, {"name": "customer"},
                   {"name": "profile"}, {"name": "user"}],
        "columns": [
            {"table": "order", "name": "id", "type": "INT", "pk": True},
            {"table": "order", "name": "customer_id", "type": "INT", "pk": False},
            {"table": "customer", "name": "id", "type": "INT", "pk": True},
            {"table": "profile", "name": "user_id", "type": "INT", "pk": True},
            {"table": "user", "name": "id", "type": "INT", "pk": True},
        ],
    }
    cands = _generate_candidate_pairs(schema)
    n1 = next(c for c in cands if c["from_table"] == "order" and c["from_col"] == "customer_id")
    assert n1["to_table"] == "customer" and n1["to_col"] == "id"
    assert n1["cardinality"] == "n:1"
    one1 = next(c for c in cands if c["from_table"] == "profile" and c["from_col"] == "user_id")
    assert one1["to_table"] == "user" and one1["cardinality"] == "1:1"


# ---------- 段5：图谱第二轮并入低置信自检 ----------


def _graph_schema_v2() -> dict:
    """单数表名：order_item.order_id → base "order" 能命中程序启发式候选。"""
    return {
        "tables": [{"name": "order"}, {"name": "customer"},
                   {"name": "product"}, {"name": "order_item"}],
        "columns": [
            {"table": "order", "name": "id", "type": "INT", "pk": True},
            {"table": "order", "name": "customer_id", "type": "INT", "pk": False},
            {"table": "customer", "name": "id", "type": "INT", "pk": True},
            {"table": "product", "name": "id", "type": "INT", "pk": True},
            {"table": "order_item", "name": "id", "type": "INT", "pk": True},
            {"table": "order_item", "name": "order_id", "type": "INT", "pk": False},
            {"table": "order_item", "name": "product_id", "type": "INT", "pk": False},
        ],
        "foreign_keys": [],
    }


class _GraphProv:
    """假 provider：第一轮全局扫描（high/medium 各一），第二轮按候选裁决（全 confirmed）。"""

    def __init__(self) -> None:
        self.rounds: list[tuple[str, dict]] = []

    async def chat(self, messages, tools=None, ctx=None):
        prompt = messages[0]["content"]
        self.rounds.append((prompt, ctx))
        if "【待裁决候选关系】" in prompt:
            return type("R", (), {"content": json.dumps([
                {"from_table": "order_item", "from_col": "order_id", "to_table": "order",
                 "to_col": "id", "cardinality": "n:1", "confidence": "high",
                 "reason": "明细归主表", "status": "confirmed"},
                {"from_table": "order_item", "from_col": "product_id", "to_table": "product",
                 "to_col": "id", "cardinality": "n:1", "confidence": "high",
                 "reason": "明细关联商品", "status": "confirmed"},
            ])})()
        return type("R", (), {"content": json.dumps([
            {"from_table": "order", "from_col": "customer_id", "to_table": "customer",
             "to_col": "id", "cardinality": "n:1", "confidence": "high", "reason": "订单归属客户"},
            {"from_table": "order_item", "from_col": "product_id", "to_table": "product",
             "to_col": "id", "cardinality": "n:1", "confidence": "medium", "reason": "低置信发现"},
        ])})()


async def test_annotate_graph_round2_includes_low_confidence_llm_edges(app_state, monkeypatch):
    """自检开（默认）：第二轮池 = 程序候选 ∪ 首轮 low/medium 置信 LLM 边；高置信边直用。"""
    from app.knowledge import annotator as ann

    prov = _GraphProv()
    monkeypatch.setattr(ann.gw, "is_effective_mock", lambda cfg: False)
    monkeypatch.setattr(ann.gw, "build_provider", lambda cfg: prov)
    edges = await ann.annotate_graph(app_state, "c", _graph_schema_v2())
    # 高置信全局边不进第二轮、原样保留
    assert any(e["from_col"] == "customer_id" and e["confidence"] == "high" for e in edges)
    # 第二轮 prompt：含低置信 product_id 边 + 未覆盖的程序候选 order_id；只裁决不新增
    assert len(prov.rounds) == 2
    verify_prompt, verify_ctx = prov.rounds[1]
    assert verify_ctx["status"] == "egress-graph-verify"
    assert "order_item.product_id → product.id" in verify_prompt
    assert "order_item.order_id → order.id" in verify_prompt
    assert "不得新增列表之外的关系" in verify_prompt and '"status":"confirmed/rejected"' in verify_prompt
    # 结果含两条第二轮通过的边
    keys = {(e["from_table"], e["from_col"], e["to_table"], e["to_col"]) for e in edges}
    assert ("order_item", "product_id", "product", "id") in keys
    assert ("order_item", "order_id", "order", "id") in keys


async def test_annotate_graph_selfcheck_off_only_program_candidates(app_state, monkeypatch):
    """kb_build_self_check=False：低置信 LLM 边不进复验池，原样保留；第二轮只验程序候选。"""
    from app.knowledge import annotator as ann

    st = app_state
    st.runtime.update({"kb_build_self_check": False})
    prov = _GraphProv()
    monkeypatch.setattr(ann.gw, "is_effective_mock", lambda cfg: False)
    monkeypatch.setattr(ann.gw, "build_provider", lambda cfg: prov)
    edges = await ann.annotate_graph(st, "c", _graph_schema_v2())
    assert len(prov.rounds) == 2  # 程序候选 order_id 仍需裁决（旧行）
    verify_prompt = prov.rounds[1][0]
    assert "order_item.order_id → order.id" in verify_prompt
    assert "order_item.product_id → product.id" not in verify_prompt  # 未进复验池
    keys = {(e["from_table"], e["from_col"], e["to_table"], e["to_col"]) for e in edges}
    assert ("order_item", "product_id", "product", "id") in keys  # 低置信边照旧保留


async def test_annotate_graph_selfcheck_param_overrides_runtime(app_state, monkeypatch):
    """显式 self_check=False 覆盖运行时开：低置信 LLM 边不进复验池。"""
    from app.knowledge import annotator as ann

    st = app_state
    st.runtime.update({"kb_build_self_check": True})  # 运行时开
    prov = _GraphProv()
    monkeypatch.setattr(ann.gw, "is_effective_mock", lambda cfg: False)
    monkeypatch.setattr(ann.gw, "build_provider", lambda cfg: prov)
    edges = await ann.annotate_graph(
        st, "c", _graph_schema_v2(), self_check=False,
    )
    verify_prompt = prov.rounds[1][0]
    assert "order_item.product_id → product.id" not in verify_prompt  # 显式关 → 不并入低置信
    assert "order_item.order_id → order.id" in verify_prompt          # 程序候选照旧


class _GraphProvVerifyFail:
    """假 provider：第一轮全局扫描正常，第二轮候选裁决抛异常（模拟超时）。"""

    def __init__(self) -> None:
        self.rounds = 0

    async def chat(self, messages, tools=None, ctx=None):
        self.rounds += 1
        if "【待裁决候选关系】" in messages[0]["content"]:
            raise RuntimeError("verify ReadTimeout")
        return type("R", (), {"content": json.dumps([
            {"from_table": "order", "from_col": "customer_id", "to_table": "customer",
             "to_col": "id", "cardinality": "n:1", "confidence": "high", "reason": "订单归属客户"},
            {"from_table": "order_item", "from_col": "product_id", "to_table": "product",
             "to_col": "id", "cardinality": "n:1", "confidence": "medium", "reason": "低置信发现"},
        ])})()


async def test_annotate_graph_verify_failure_falls_back_high_conf(app_state, monkeypatch):
    """裁决轮抛异常 → 不致命：回退全局高置信边（低置信边丢弃，不再把图边弄丢）。"""
    from app.knowledge import annotator as ann

    prov = _GraphProvVerifyFail()
    monkeypatch.setattr(ann.gw, "is_effective_mock", lambda cfg: False)
    monkeypatch.setattr(ann.gw, "build_provider", lambda cfg: prov)
    edges = await ann.annotate_graph(app_state, "c", _graph_schema_v2())  # 不应抛
    assert len(edges) >= 1, "全局高置信边应保留"
    assert any(e["from_col"] == "customer_id" and e["confidence"] == "high" for e in edges)
    assert not any(e.get("source") == "llm_verify" for e in edges), "裁决失败 → 无通过项"
    assert prov.rounds == 2  # 全局 + 裁决（裁决失败但已调用）
