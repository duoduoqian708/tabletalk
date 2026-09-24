"""任务开发工具（ReAct 候选工作区）：candidate_write / candidate_read / candidate_test。

这是 harness 式 read_file/write_file/exec_command 的平台原生等价物，只对每次任务创作会话的
**专属工作区**（data_dir/task_ws/<session_id>/candidate.py）生效：
- 写：把 agent 的当前最佳版本写入候选文件；
- 读：读回当前候选（改哪儿看哪儿，防长脚本漂移）；
- 测：把候选跑进双重沙箱（jobs/ 副本 + sandbox-exec），报错/日志/摘要回灌给 agent 自己调试。

真实 jobs/ 目录与 deploy 管线不在此列：写盘唯一路径仍是 deploy（人确认），模型只有文本进出。
"""
from __future__ import annotations

import contextvars

from app.ai.tools.registry import ToolOutcome, register_tool

# 当前任务创作会话的工作区目录（由 task-agent 的 ReAct 循环在执行工具前注入）
_WS: contextvars.ContextVar[str | None] = contextvars.ContextVar("task_ws", default=None)


def set_workspace(path: str | None):
    return _WS.set(path)


def reset_workspace(token):
    _WS.reset(token)


def _candidate_path():
    ws = _WS.get()
    if not ws:
        return None
    from pathlib import Path

    return Path(ws) / "candidate.py"


async def _candidate_write(state, args, conn_id, include_data=False):
    from pathlib import Path

    script = (args or {}).get("script", "")
    if not script.strip():
        return ToolOutcome(result={"ok": False, "error": "script 不能为空"})
    path = _candidate_path()
    if path is None:
        return ToolOutcome(result={"ok": False, "error": "候选工作区不存在（会话未初始化）"})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script, encoding="utf-8")
    lines = script.count("\n") + 1
    return ToolOutcome(result={"ok": True, "lines": lines, "bytes": path.stat().st_size,
                               "note": "已保存。禁止连续重写——立刻 candidate_test 看结果，有报错再改。"})


async def _candidate_read(state, args, conn_id, include_data=False):
    path = _candidate_path()
    if path is None:
        return ToolOutcome(result={"ok": False, "error": "候选工作区不存在"})
    if not path.exists():
        return ToolOutcome(result={"ok": False, "error": "候选文件还不存在，先 candidate_write 写入"})
    text = path.read_text(encoding="utf-8", errors="replace")
    return ToolOutcome(result={"ok": True, "script": text, "lines": text.count("\n") + 1})


async def _candidate_test(state, args, conn_id, include_data=False):
    path = _candidate_path()
    if path is None:
        return ToolOutcome(result={"ok": False, "error": "候选工作区不存在"})
    if not path.exists():
        return ToolOutcome(result={"ok": False, "error": "候选文件还不存在，先 candidate_write 写入再测"})
    if not conn_id:
        return ToolOutcome(result={"ok": False, "error": "缺少连接（connection_id），无法测试"})
    from app.tasks.runner import run_job_raw

    script = path.read_text(encoding="utf-8", errors="replace")
    # 完全复用线上测试路径：同一双重沙箱，候选字节原样执行（代表性即生产）
    result = await run_job_raw(state, script, conn_id, timeout=30)
    return ToolOutcome(result=result)


def register():
    register_tool(
        "candidate_write",
        "把候选脚本的当前完整版本整套写入你的工作文件（candidate.py），覆盖旧版。"
        "硬规则：写完后必须立刻 candidate_test 验证；没有看到测试结果绝不能再写。"
        "只有读到测试报错、需要针对性修改时，才再次写入。",
        {"script": {"type": "string", "description": "完整 Python 脚本源码"}},
        ["script"],
        _candidate_write,
        trust="mutating",
    )
    register_tool(
        "candidate_read",
        "读回你的候选脚本当前内容（逐行返回）。改复杂脚本时先读再改，避免遗漏或改动漂移。",
        {},
        [],
        _candidate_read,
        trust="readonly",
    )
    register_tool(
        "candidate_test",
        "在隔离沙箱中运行你的候选脚本并返回结果（stdout/摘要/报错/退出码）。"
        "沙箱是 jobs/ 的一次性副本 + macOS 沙箱，无法改动任何真实文件；SQL 照常过安全闸门。"
        "报错就把错误信息告诉下一步怎么修，改完再测，直到 ok=true。",
        {},
        [],
        _candidate_test,
        trust="mutating",
    )