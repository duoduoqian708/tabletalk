"""2026-09 构建体验修复回归：阶段一空跑打满 / 两段式爬坡 / 容忍式部分 JSON 计数 / 流式消费。"""
from __future__ import annotations

import asyncio

import pytest

from app.knowledge.annotator import (
    _chat_with_beat,
    _climb_pct,
    _count_json_items,
)
from app.ai.gateway import ChatResponse, StreamChunk


async def _schema(app_state, conn_id: str) -> dict:
    from app.core.schema import get_schema
    return await get_schema(app_state, conn_id)


class _FakeStreamProvider:
    """流式假 provider：逐 delta 吐 JSON 数组（含字符串内括号/转义），记录 chat 调用。

    reasoning=True → 前几块带思考链 delta（测 live 尾巴）。
    """

    def __init__(self, pieces: list[str], reasoning: list[str] | None = None) -> None:
        self._pieces = pieces
        self._reasoning = reasoning or []
        self.chat_calls = 0

    async def chat(self, messages, tools=None, ctx=None):
        self.chat_calls += 1
        return ChatResponse(content="".join(self._pieces))

    async def chat_stream(self, messages, tools=None, ctx=None):
        for r in self._reasoning:
            yield StreamChunk(delta=None, reasoning=r)
            await asyncio.sleep(0.6)  # > 推帧节流 0.5s，模拟真实推理节奏
        for p in self._pieces:
            yield StreamChunk(delta=p)
            await asyncio.sleep(0.6)


class _NoStreamProvider:
    async def chat(self, messages, tools=None, ctx=None):
        return ChatResponse(content='[{"a":1}]')


def test_climb_pct_two_phase():
    """两段式爬坡：0s=p_from；20s=半窗；climb 结束=p_to-1；全程单调不超界。"""
    assert _climb_pct(0, 50, 100, 380) == 50
    assert _climb_pct(20, 50, 100, 380) == 75
    assert _climb_pct(400, 50, 100, 380) == 99
    assert _climb_pct(100000, 50, 100, 380) == 99
    prev = -1
    for i in range(0, 5000):
        v = _climb_pct(i / 10.0, 50, 100, 380)
        assert v >= prev and 50 <= v <= 99
        prev = v


def test_count_json_items_tolerant():
    """容忍式计数：未闭合对象也计（至少 N 语义）；字符串/转义内括号不干扰；前置文字容忍。"""
    cases = [
        ('[{"a":1},{"b":2},{"c":3', 3),
        ('', 0),
        ('{"a": "x[1]{2}"}', 0),
        ('[{"r":"a\\"b}]"},{"t":"[x]"}]', 2),
        ('好的，以下是结果：[{"a":1}]', 1),
        ('[', 0),
        ('[{},{}]', 2),
    ]
    for s, want in cases:
        assert _count_json_items(s) == want, repr(s)


async def test_chat_with_beat_stream_counts():
    """流式消费：返回完整 content；detail 出真实计数帧。"""
    frames: list[tuple] = []
    provider = _FakeStreamProvider(['好的：[', '{"a":1},', '{"b":2},{"c"', ':3}]'])
    resp = await _chat_with_beat(
        provider, [{"role": "user", "content": "x"}], None, {"conn_id": "t"},
        on_progress=lambda stage, pct, detail, **kw: frames.append((pct, detail, kw.get("live"))),
        stage="AI 关系识别", phase="graph", step="global",
        step_index=1, step_total=1, percent=0, p_to=50,
        climb_seconds=1.0, stream=True, count_detail="已识别 {n} 条关系",
    )
    assert resp.content == '好的：[{"a":1},{"b":2},{"c":3}]'
    counted = [d for _, d, _ in frames if "已识别" in d]
    assert counted, frames
    assert "已识别 3 条关系" in counted[-1]
    assert provider.chat_calls == 0, "流式路径不应回退 chat()"


async def test_chat_with_beat_stream_live_tail():
    """live 尾巴：思考链先行（live=思考尾文），正文开始后切内容尾；单行无换行。"""
    frames: list[tuple] = []
    provider = _FakeStreamProvider(
        ['[{"a"', ':1}]'],
        reasoning=["我需要分析orders表的", "\n外键关系，先看customer_id。"],
    )
    resp = await _chat_with_beat(
        provider, [{"role": "user", "content": "x"}], None, {"conn_id": "t"},
        on_progress=lambda stage, pct, detail, **kw: frames.append(kw.get("live")),
        stage="AI 关系识别", phase="graph", step="global",
        step_index=1, step_total=1, percent=0, p_to=50,
        climb_seconds=1.0, stream=True, count_detail=None,
    )
    assert resp.content == '[{"a":1}]'
    assert frames, "应至少收到一帧"
    assert all(f is not None for f in frames), f"全程应有 live 尾巴：{frames}"
    assert any("外键关系" in f for f in frames), f"思考尾巴应出现：{frames}"
    assert any("a" in f for f in frames), f"正文尾巴应出现：{frames}"
    assert all("\n" not in f for f in frames), f"live 必须单行：{frames}"


async def test_report_live_pipeline(app_state, conn_id):
    """report 管道：live 写入 progress 对应 phase（含 busy 帧与 done 帧路径）。"""
    from app.knowledge.jobs import BuildJob, run_build_job

    captured: list[dict] = []

    async def build_fn(report):
        report("AI 关系识别", 30, "AI 思考中", phase="graph", step="global",
               step_index=1, step_total=1, busy=True, live="已识别 3 条关系 · orders→customers")
        captured.append(dict(app_state.build_jobs.progress(conn_id)))
        return {"degraded_phases": []}

    job = app_state.build_jobs.start(conn_id, build_fn, kind="build")
    for _ in range(100):
        if job.task.done():
            break
        await asyncio.sleep(0.02)
    graph_phase = captured[0]["phases"][2]  # phases 顺序 = PHASES 定义
    assert graph_phase["key"] == "graph"
    assert graph_phase["live"] == "已识别 3 条关系 · orders→customers"
    assert graph_phase["busy"] is True


async def test_chat_with_beat_no_stream_provider_falls_back():
    """provider 无 chat_stream → 回退非流式 chat()，结果不变。"""
    provider = _NoStreamProvider()
    resp = await _chat_with_beat(
        provider, [{"role": "user", "content": "x"}], None, {"conn_id": "t"},
        on_progress=lambda *a, **k: None,
        stage="s", phase="graph", step="global",
        step_index=1, step_total=1, percent=0, p_to=50, stream=True,
    )
    assert resp.content == '[{"a":1}]'


async def test_diff_rebuild_annotates_skip_frame(app_state, conn_id):
    """无变更 diff 重构：阶段一收到 100% + 跳过提示帧，不再挂 0%。"""
    st = app_state
    await st.knowledge.build(conn_id, await _schema(st, conn_id), None)
    await st.knowledge.confirm_all(conn_id)
    frames: list[tuple] = []

    def report(stage, percent, detail=None, **kw):
        if kw.get("phase") == "annotate":
            frames.append((percent, detail))

    await st.knowledge.build(conn_id, await _schema(st, conn_id), None,
                             annotate_mode="diff", tag_mode="keep",
                             on_progress=report)
    assert any(pct == 100 and detail and "跳过" in detail for pct, detail in frames), frames


def test_deterministic_candidates_block(app_state, conn_id):
    """候选块：FK/命名边 → 表对级清单；无候选 → 空串（不注入区块）。"""
    from app.knowledge.annotator import _deterministic_candidates_block
    schema = {
        "tables": [{"name": "orders", "column_count": 3}, {"name": "customers", "column_count": 3}],
        "columns": [
            {"table": "orders", "name": "id", "type": "INTEGER", "pk": True},
            {"table": "orders", "name": "customer_id", "type": "INTEGER"},
            {"table": "customers", "name": "id", "type": "INTEGER", "pk": True},
        ],
        "foreign_keys": [
            {"table": "orders", "column": "customer_id", "ref_table": "customers", "ref_column": "id"},
        ],
    }
    block = _deterministic_candidates_block(schema)
    assert "orders.customer_id → customers.id" in block
    assert "FK" in block
    # 无 FK/无命名信号的库 → 空串（prompt 不出区块）
    empty = _deterministic_candidates_block({"tables": [], "columns": [], "foreign_keys": []})
    assert empty == ""


def test_relation_prompt_injects_candidates():
    """全局扫描 prompt：候选块注入 __CANDIDATES_BLOCK__；render 缺参留空（EN 模板占位符对齐回归）。"""
    from app.ai.prompts import render
    p = render("annotator_relation_global", graph_overview="PORTRAITS",
               candidates_block="\n【代码已发现的候选（确定性规则产出）】\n- a.x → b.y（FK，n:1）\n")
    assert "PORTRAITS" in p and "a.x → b.y" in p and "程序候选参考" in p
    # 不传候选块 → 占位符替换为空，模板结构完整
    p2 = render("annotator_relation_global", graph_overview="PORTRAITS", candidates_block="")
    assert "PORTRAITS" in p2 and "__CANDIDATES_BLOCK__" not in p2
    # EN 模板（旧版占位符 __TABLE_STRUCTURES__ 未修复时此断言失败）
    pe = render("annotator_relation_global", graph_overview="PORTRAITS",
                candidates_block="", locale="en")
    assert "PORTRAITS" in pe and "__GRAPH_OVERVIEW__" not in pe and "__CANDIDATES_BLOCK__" not in pe
