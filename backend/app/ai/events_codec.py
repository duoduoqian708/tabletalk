"""SSE 事件 → 会话消息行 编解码（committed-turn 逐轮持久化的共享转换器）。

harness.py（run_react_loop）与 report.py（report_stream）都需要把已产出的事件转换为
chat.db messages 行；放独立模块避免 loop↔report 循环导入。
"""
from __future__ import annotations

import json
from typing import Any


def ai_messages_from_events(events: list[dict]) -> list[dict[str, Any]]:
    """把已产出的 SSE 事件（text/think/stage/sql_card/plan/section/narration/clarify）
    转换为可落库的 message 行（user 消息由请求层单独提交，不在此处理）。"""
    out: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for ev in events:
        t = ev.get("type")
        if t == "text":
            text_parts.append(ev.get("content", ""))
        elif t == "think":
            if text_parts:
                out.append({"role": "assistant", "kind": "text", "content": "".join(text_parts)})
                text_parts = []
            out.append({"role": "assistant", "kind": "think", "content": ev.get("text", "")})
        elif t == "stage":
            out.append({"role": "assistant", "kind": "stage", "content": json.dumps(ev, ensure_ascii=False)})
        elif t == "sql_card":
            if text_parts:
                out.append({"role": "assistant", "kind": "text", "content": "".join(text_parts)})
                text_parts = []
            card = ev.get("card", {})
            out.append({
                "role": "assistant", "kind": "sql_card",
                "content": json.dumps(card, ensure_ascii=False),
                "sql": card.get("sql", ""), "verdict": card.get("verdict", ""),
            })
        elif t in ("plan", "section", "narration"):
            out.append({"role": "assistant", "kind": "report", "content": json.dumps(ev, ensure_ascii=False)})
        elif t == "clarify":
            if text_parts:
                out.append({"role": "assistant", "kind": "text", "content": "".join(text_parts)})
                text_parts = []
            out.append({"role": "assistant", "kind": "clarify", "content": ev.get("question", "")})
    if text_parts:
        out.append({"role": "assistant", "kind": "text", "content": "".join(text_parts)})
    return out
