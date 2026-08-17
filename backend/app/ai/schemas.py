"""AI 层请求/事件 pydantic 模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None


class ChatRequest(BaseModel):
    connection_id: str
    messages: list[ChatMessage] = Field(default_factory=list)
    # 会话管理：前端传会话 id（新建时为 None，后端生成并返回）；标题前端异步生成后回传
    session_id: str | None = None
    title: str | None = None
    # 模式分流：query（默认，单查询）| report（分析报告：澄清→计划→逐章执行→汇总）
    # 前端「报告」按钮显式传 "report" 绕过意图分类；默认 None 由意图分类决定
    mode: str | None = None
    # 模型选择：model_id 命中 ai_models 时优先（"按对话切模型"）；为空则走默认模型
    model_id: str | None = None
    # 逐次覆盖网关（支持"网关下拉即切"）
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    # 推理思考开关（None=跟随模型配置；显式传 True/False 覆盖本次对话）
    reasoning: bool | None = None
    # 结果回传 opt-in（隐私红线：默认只回列名+行数）
    include_data: bool = False
    table: str | None = None


class SelectionRequest(BaseModel):
    connection_id: str
    sql: str
    kind: str = "explain"          # explain | optimize | risk
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
