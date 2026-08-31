"""E1 引擎任务规划模型测试（设计 §13.1/§13.2）。"""
from __future__ import annotations

import pytest

from app.ai.plan import MODALITY_ORDER, VALID_ACTIONS, TaskPlan, TaskSpec, trust_for


# ---------- action 封闭枚举 ----------

def test_valid_actions_closed_set():
    assert VALID_ACTIONS == {"query", "write", "ddl", "kb", "schedule", "system", "unknown"}
    assert "unknown" in VALID_ACTIONS  # 兜底值必须存在


def test_unknown_action_normalized():
    t = TaskSpec(action="banana")  # 非法 action → unknown
    assert t.action == "unknown"
    assert t.trust == "read"  # unknown 派生 read（general 承接低危）


# ---------- modality 有序枚举 ----------

def test_modality_order():
    assert MODALITY_ORDER == ("answer", "analyze", "report", "automate")


def test_invalid_modality_normalized():
    t = TaskSpec(action="query", modality="banana")
    assert t.modality == "answer"


# ---------- trust 派生（§13.2#1：不分类，查表） ----------

def test_trust_derived_from_action():
    assert trust_for("query") == "read"
    assert trust_for("write") == "write"
    assert trust_for("ddl") == "ddl"
    assert trust_for("kb") == "write"
    assert trust_for("schedule") == "write"
    assert trust_for("unknown") == "read"
    assert trust_for("system") == "read"  # F6：system 未测补全


def test_task_trust_property():
    assert TaskSpec(action="query").trust == "read"
    assert TaskSpec(action="write").trust == "write"


# ---------- 序列化往返 ----------

def test_to_from_dict_roundtrip():
    t = TaskSpec(action="query", modality="report", target={"tables": ["orders"]}, id="t1")
    d = t.to_dict()
    t2 = TaskSpec.from_dict(d)
    assert t2.action == "query" and t2.modality == "report"
    assert t2.target == {"tables": ["orders"]} and t2.id == "t1"
    assert t2.trust == "read"


# ---------- TaskPlan ----------

def test_plan_empty_defaults_to_query():
    p = TaskPlan()
    assert len(p.tasks) == 1
    assert p.tasks[0].action == "query"
    assert p.tasks[0].modality == "answer"


def test_plan_has_write():
    p = TaskPlan(tasks=[TaskSpec(action="query"), TaskSpec(action="write")])
    assert p.has_write is True
    assert TaskPlan(tasks=[TaskSpec(action="query")]).has_write is False


def test_plan_to_dict():
    p = TaskPlan(
        tasks=[TaskSpec(action="query", modality="analyze")],
        tags=["订单"], followup_tables=["orders"],
        skip_retrieval=True, degraded=True,
    )
    d = p.to_dict()
    assert d["tasks"][0]["action"] == "query"
    assert d["tags"] == ["订单"] and d["skip_retrieval"] is True and d["degraded"] is True


def test_plan_composite_tasks():
    """复合请求：2 个独立任务（设计 §13.3：独立操作才拆）。"""
    p = TaskPlan(tasks=[
        TaskSpec(action="query", modality="answer", target={"tables": ["orders"]}, id="t1"),
        TaskSpec(action="query", modality="report", target={"tables": ["users"]}, id="t2"),
    ])
    assert len(p.tasks) == 2
    assert p.tasks[1].modality == "report"  # 升降级沿有序枚举


def test_plan_from_dict():
    """F6：TaskPlan.from_dict 往返（序列化兼容）。"""
    p = TaskPlan(tasks=[
        TaskSpec(action="query", modality="answer", target={"tables": ["orders"]}, id="t1"),
        TaskSpec(action="write", modality="answer", id="t2"),
    ], tags=["订单"])
    d = p.to_dict()
    p2 = TaskPlan.from_dict(d)
    assert [t.action for t in p2.tasks] == ["query", "write"]
    assert p2.tasks[0].target == {"tables": ["orders"]}
    assert p2.tags == ["订单"]