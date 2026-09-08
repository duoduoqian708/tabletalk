"""定时任务 API（全脚本化）：任务 = jobs/*.py 脚本，声明头驱动注册。

创建：POST /tasks/deploy（AI 对话产出的脚本落盘）；列表/启停/编辑/删除全作用于脚本文件。
运行记录在 jobs.db（JobStore）；执行与审计见 app/tasks/runner.py。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.ai.tools import execute_tool
from app.state import get_state
from app.tasks import cron

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

_HEAD_RE = re.compile(r"#\s*([A-Za-z]+)\s*:\s*(.*)")

# 任务创作会话的工具白名单（harness 式 ReAct agent）：
# get_schema（查表结构）+ candidate_write/read/test（候选工作区：写→读→沙箱自测）。
# 工具全程常开——harness 的本质是"工具自由 + 结果回灌"，不用阶段白名单限制调试能力；
# "写而不测"由交付硬拦截兜底（见 task_agent 循环）。
# 写盘唯一路径仍是 deploy（人确认 + 服务端强制沙箱测试）；测试跑双重沙箱物理隔离。
_TASK_TOOLS = []
def _init_task_tools() -> None:
    global _TASK_TOOLS
    if _TASK_TOOLS:
        return
    from app.ai.tools.registry import TOOL_SCHEMAS

    _TASK_TOOLS = [t for t in TOOL_SCHEMAS if t["function"]["name"] in
                   ("get_schema", "candidate_write", "candidate_read", "candidate_test")]

_init_task_tools()


def _slug(name: str) -> str:
    s = re.sub(r"[^\w一-鿿]+", "_", (name or "").strip()).strip("_")
    return (s or "task")[:40] + ".py"


def _resolve_connection(state, key: str) -> tuple[str, str]:
    """连接 id 或 name → (id, name)。空串原样返回；查不到抛 ValueError（绝不静默猜）。"""
    k = (key or "").strip()
    if not k:
        return "", ""
    conns = state.connections.list()
    for c in conns:
        if c.id == k:
            return c.id, c.name
    for c in conns:
        if c.name == k:
            return c.id, c.name
    raise ValueError(f"连接不存在: {k}")


def _compile_check(script: str) -> None:
    """py_compile 语法校验：失败抛 HTTPException(400)。"""
    import os as _os
    import py_compile
    import tempfile

    tmp = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tf:
            tf.write(script)
            tmp = tf.name
        py_compile.compile(tmp, doraise=True)
    except py_compile.PyCompileError as e:
        raise HTTPException(status_code=400, detail=f"脚本语法错误: {str(e)[:800]}")
    finally:
        if tmp:
            try:
                _os.unlink(tmp)
            except OSError:
                pass


def _job_dict(state, job) -> dict[str, Any]:
    last = state.job_store.last(job.name)
    nxt = cron.next_after(job.cron, _dt.datetime.now())
    return {
        "name": job.name,
        "cron": job.cron,
        "friendly": cron.friendly(job.cron),
        "connection": job.connection,
        "enabled": job.enabled,
        "system": job.system,
        "file": job.file,
        "next_run": nxt.isoformat() if nxt else None,
        "last_run": last["started_at"] if last else None,
        "last_status": last["status"] if last else None,
        "last_summary": last["summary"] if last else None,
    }


class DeployBody(BaseModel):
    name: str
    cron: str
    connection: str = ""
    script: str = ""
    overwrite: bool = False


class TaskPatch(BaseModel):
    enabled: bool | None = None
    cron: str | None = None


class ScriptBody(BaseModel):
    script: str


class CronPreview(BaseModel):
    cron: str


@router.get("")
async def list_tasks() -> dict[str, Any]:
    state = get_state()
    state.jobs.reload()
    jobs = sorted(state.jobs.list(), key=lambda j: (not j.system, j.name))
    from app.tasks.runner import running_jobs

    running_set = set(running_jobs())
    tasks = []
    for j in jobs:
        d = _job_dict(state, j)
        d["running"] = j.name in running_set
        tasks.append(d)
    return {"ok": True, "tasks": tasks, "count": len(tasks)}


@router.post("/cron/preview")
async def cron_preview(body: CronPreview) -> dict[str, Any]:
    nxt = cron.next_after(body.cron, _dt.datetime.now())
    if nxt is None:
        raise HTTPException(status_code=400, detail=f"无效 cron 表达式: {body.cron}")
    return {"ok": True, "cron": body.cron, "friendly": cron.friendly(body.cron), "next_run": nxt.isoformat()}


@router.post("/deploy")
async def deploy(body: DeployBody) -> dict[str, Any]:
    """新任务落盘：AI 对话产出的脚本写入 jobs/。

    部署门禁（服务端强制，不信任前端）：py_compile 语法校验 + 双重沙箱实测一次
    （TABLETALK_TEST=1：写操作仅 dry-run 预览，SELECT 真跑）。任一失败 → 400 + 测试输出。
    通过后落盘，默认 enabled:false——用户"立即运行"确认输出符合预期后再启用。
    """
    state = get_state()
    name = (body.name or "").strip()
    cron_s = (body.cron or "").strip()
    if not name or not cron_s:
        raise HTTPException(status_code=400, detail="任务名与 cron 必填")
    if cron.next_after(cron_s, _dt.datetime.now()) is None:
        raise HTTPException(status_code=400, detail=f"无效 cron: {cron_s}")

    conn_id, conn_name = "", ""
    try:
        conn_id, conn_name = _resolve_connection(state, body.connection or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    target = state.jobs.dir / _slug(name)
    if target.exists() and not body.overwrite:
        raise HTTPException(status_code=409, detail=f"同名脚本已存在（{target.name}），如需覆盖请带 overwrite=true")

    text = (body.script or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="script 不能为空")
    _compile_check(text)

    # ── 部署门禁：沙箱实测（写 dry-run，读真跑），失败拒绝落盘 ──
    from app.tasks.runner import run_job_raw

    test = await run_job_raw(state, text, conn_name or conn_id, timeout=30)
    if not test.get("ok"):
        out = str(test.get("output") or "")
        tail = "\n".join(out.splitlines()[-12:])[:1200] if out else str(test.get("summary") or "")
        raise HTTPException(
            status_code=400,
            detail=f"部署门禁：脚本沙箱测试未通过（{test.get('status')}）。请修复后重试。\n{tail}",
        )

    # 覆盖已有任务时保留原 enabled 状态；新任务默认停用（用户确认输出后再启用）
    keep_enabled = "true" if (target.exists() and _header_enabled(target.read_text(encoding="utf-8", errors="replace"))) else "false"

    # 保证声明头完整（name/cron必带；connection/enabled 按需补）
    header_lines = text.splitlines()[:40]
    have = {m.group(1) for m in (_HEAD_RE.match(l) for l in header_lines) if m}
    prepend: list[str] = []
    for k, v in (("name", name), ("cron", cron_s)):
        if k not in have:
            prepend.append(f"# {k}: {v}")
    if "connection" not in have and body.connection:
        prepend.append(f"# connection: {body.connection}")
    if "enabled" not in have:
        prepend.append(f"# enabled: {keep_enabled}")
    if prepend:
        text = "\n".join(prepend) + "\n" + text

    target.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
    state.jobs.reload()

    job = state.jobs.get(name)
    if not job:
        # 头 name 与参数不一致 → 强制头 name，再重扫
        from app.tasks.jobs import rewrite_header

        rewrite_header(target, {"name": name})
        state.jobs.reload()
        job = state.jobs.get(name)
    if not job:
        raise HTTPException(status_code=500, detail="脚本未注册为任务（请确认脚本头含合法 # name: 与 # cron:）")
    return {
        "ok": True,
        "task": _job_dict(state, job),
        "test": {"ok": test.get("ok"), "status": test.get("status"), "summary": test.get("summary")},
    }


def _header_enabled(script_text: str) -> bool:
    for line in script_text.splitlines()[:40]:
        m = _HEAD_RE.match(line)
        if m and m.group(1) == "enabled":
            return m.group(2).strip().lower() in ("true", "1", "yes", "on")
    return True


@router.get("/{name}/runs")
async def task_runs(name: str) -> dict[str, Any]:
    state = get_state()
    if not state.jobs.has(name):
        raise HTTPException(status_code=404, detail=f"任务 {name} 不存在")
    return {"ok": True, "name": name, "runs": state.job_store.recent_runs(name)}


_REPORT_NAME_RE = re.compile(r"^[\w\-.一-鿿]+$")


@router.get("/reports/{filename}")
async def read_report(filename: str) -> dict[str, Any]:
    """读取任务产出物（data_dir/reports/ 下的报表文件），供前端预览。

    文件名白名单校验 + resolve 后确认父目录，防路径穿越。
    """
    if not _REPORT_NAME_RE.fullmatch(filename or ""):
        raise HTTPException(status_code=400, detail="非法文件名")
    state = get_state()
    reports_dir = (state.env.data_dir / "reports").resolve()
    target = (reports_dir / filename).resolve()
    if target.parent != reports_dir:
        raise HTTPException(status_code=400, detail="非法路径")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"报表不存在: {filename}")
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"读取报表失败: {e}")
    stat = target.stat()
    return {
        "ok": True,
        "name": filename,
        "size": stat.st_size,
        "mtime": _dt.datetime.fromtimestamp(stat.st_mtime, _dt.timezone.utc).isoformat(),
        "content": content,
    }


@router.get("/{name}/script")
async def task_script(name: str) -> dict[str, Any]:
    """读取脚本原文（编辑脚本弹窗用）。"""
    state = get_state()
    job = state.jobs.get(name)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 {name} 不存在")
    f = state.jobs.file(name)
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"读取脚本失败: {e}")
    return {"ok": True, "name": name, "script": text, "cron": job.cron, "connection": job.connection}


@router.post("/test")
async def test_task_script(body: dict) -> dict[str, Any]:
    """在隔离沙箱中执行脚本一次并返回结果（不保存任务、不改任何真实文件）。

    写操作自动 dry-run 预览（TABLETALK_TEST=1）；SELECT 真跑。connection_id 接受 id 或名称。
    """
    from app.tasks.runner import run_job_raw

    script = body.get("script", "")
    connection_key = body.get("connection_id", "")
    if not script.strip():
        raise HTTPException(status_code=400, detail="script 不能为空")
    state = get_state()
    try:
        _cid, conn_name = _resolve_connection(state, connection_key or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result = await run_job_raw(state, script, conn_name, timeout=30)
    return {"ok": result.get("ok", False), "status": result.get("status"), "summary": result.get("summary"), "output": result.get("output")}


@router.post("/{name}/run")
async def task_run(name: str) -> dict[str, Any]:
    """立即运行（手动触发，与到点触发共用同一执行器，不构成旁路）。"""
    state = get_state()
    if not state.jobs.has(name):
        raise HTTPException(status_code=404, detail=f"任务 {name} 不存在")
    from app.tasks.runner import run_job

    return await run_job(state, name, trigger="manual")


@router.post("/{name}/cancel")
async def task_cancel(name: str) -> dict[str, Any]:
    """取消正在运行的任务。"""
    from app.tasks.runner import cancel_job, is_running

    if not is_running(name):
        return {"ok": False, "message": "任务未在运行中"}
    ok = cancel_job(name)
    return {"ok": ok, "message": "已发送取消信号" if ok else "任务未在运行中"}


@router.put("/{name}")
async def patch_task(name: str, body: TaskPatch) -> dict[str, Any]:
    """启停 / 改 cron：改写脚本声明头（enabled/cron）。"""
    state = get_state()
    job = state.jobs.get(name)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 {name} 不存在")
    changes: dict[str, str] = {}
    if body.enabled is not None:
        changes["enabled"] = "true" if body.enabled else "false"
    if body.cron is not None:
        if cron.next_after(body.cron, _dt.datetime.now()) is None:
            raise HTTPException(status_code=400, detail=f"无效 cron: {body.cron}")
        changes["cron"] = body.cron
    if changes:
        from app.tasks.jobs import rewrite_header

        rewrite_header(state.jobs.file(name), changes)
        state.jobs.reload()
    return {"ok": True, "task": _job_dict(state, state.jobs.get(name) or job)}


@router.put("/{name}/script")
async def save_script(name: str, body: ScriptBody) -> dict[str, Any]:
    """整文件保存（AI 修订/人工编辑后）。先过语法校验；头被误删则回写 name/cron 兜底再注册。"""
    state = get_state()
    job = state.jobs.get(name)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 {name} 不存在")
    _compile_check(body.script)
    from app.tasks.jobs import parse_header, rewrite_header

    f = state.jobs.file(name)
    f.write_text(body.script, encoding="utf-8")
    state.jobs.reload()
    if not state.jobs.has(name):
        rewrite_header(f, {"name": job.name, "cron": parse_header(body.script).get("cron") or job.cron})
        state.jobs.reload()
    return {"ok": True, "task": _job_dict(state, state.jobs.get(name) or job)}


@router.delete("/{name}")
async def delete_task(name: str) -> dict[str, Any]:
    """删除任务 = 删除脚本文件。删了即消失，对平台零影响。"""
    state = get_state()
    job = state.jobs.get(name)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 {name} 不存在")
    f = state.jobs.file(name)
    if f is not None:
        try:
            f.unlink()
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"删除失败: {e}")
    state.jobs.reload()
    state.job_store.clear(name)
    return {"ok": True, "message": f"任务 {name} 已删除（脚本已移除）"}


# ── AI 任务创作会话：对话澄清 → 产脚本提案（非流式 v1；产物永远是一个 .py） ──

from app.ai.prompts import render as _render_prompt

_TASK_AGENT_PROMPT = _render_prompt("task_agent")

_TASK_AGENT_EDIT_PROMPT = _render_prompt("task_agent_edit")


class AgentMessage(BaseModel):
    role: str
    content: str


class AgentBody(BaseModel):
    messages: list[AgentMessage]
    connection_id: str = ""
    edit_task: str = ""
    session_id: str = ""  # 任务创作会话 id（前端每次打开对话框生成一次）；用于候选工作区隔离与跨轮复用
    confirm_plan: bool = False  # 前端需求确认卡「确认」按钮 → 解锁 PLAN 硬闸（服务端强制先对齐再产脚本）


# ── 会话状态（task_ws/<sid>/state.json）：跨请求持久化，断线/超时后续跑的基础 ──
# - plan_confirmed：PLAN 硬闸闸门（前端确认按钮 or 启发式：用户在 PLAN 输出后回复过）
# - passed_script  ：最近一次通过沙箱测试的候选快照（跨请求保留，未测拦截跨轮有效）
# - cumulative_*   ：会话累计护栏（单请求 MAX_ACTIONS/WALL_CLOCK 之外的第二道防无限续跑）
# - last_interrupt ：一次性中断摘要（注入下一轮后即清）
_SESSION_MAX_ACTIONS = 120
_SESSION_WALL_CLOCK = 1800.0  # 30min
_REPLAY_PLAN_MARK = "[需求登记单已输出]"  # 前端回放 assistant 气泡时附加的协议标记
_REPLAY_PROPOSAL_MARK = "[脚本提案已交付]"


def _load_ws_state(ws_dir) -> dict:
    try:
        f = ws_dir / "state.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 状态损坏按新会话处理
        pass
    return {}


def _save_ws_state(ws_dir, st: dict) -> None:
    try:
        st["updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        (ws_dir / "state.json").write_text(
            json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _parse_marker_json(text: str, marker: str) -> dict[str, Any] | None:
    """解析 '---MARKER---' + JSON（取最后一次出现；容错 ``` 包裹与前后杂讯）。"""
    parts = re.split(r"---" + marker + r"---", text)
    if len(parts) < 2:
        return None
    chunk = parts[-1].strip()
    if not chunk:
        return None
    if chunk.startswith("```"):
        chunk = re.sub(r"^```[a-zA-Z0-9-]*\s*|\s*```$", "", chunk).strip()
    try:
        data = json.loads(chunk)
    except Exception:  # noqa: BLE001
        try:
            s = chunk.index("{")
            e = chunk.rindex("}")
            data = json.loads(chunk[s : e + 1])
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def _parse_proposal(text: str) -> dict[str, Any] | None:
    data = _parse_marker_json(text, "PROPOSAL")
    if not data or not data.get("script"):
        return None
    cron_s = (data.get("cron") or "").strip()
    nxt = cron.next_after(cron_s, _dt.datetime.now()) if cron_s else None
    return {
        "script": data["script"],
        "name": (data.get("name") or "").strip(),
        "cron": cron_s,
        "connection": (data.get("connection") or "").strip(),
        "summary": (data.get("summary") or "").strip(),
        "next_run": nxt.isoformat() if nxt else None,
    }


def _parse_plan(text: str) -> dict[str, Any] | None:
    """解析 ---PLAN--- 需求登记单（澄清协议）：缺项问题驱动，先对齐再产脚本。"""
    data = _parse_marker_json(text, "PLAN")
    if not data:
        return None
    questions = [str(q).strip() for q in (data.get("questions") or []) if str(q).strip()]
    tables = [str(t).strip() for t in (data.get("tables") or []) if str(t).strip()]
    if not questions and not tables and not (data.get("cron") or "").strip():
        return None  # 空壳不算 plan
    cron_s = (data.get("cron") or "").strip()
    nxt = cron.next_after(cron_s, _dt.datetime.now()) if cron_s else None
    return {
        "cron": cron_s,
        "cron_friendly": cron.friendly(cron_s) if nxt else "",
        "connection": (data.get("connection") or "").strip(),
        "tables": tables[:20],
        "mode": ("write" if str(data.get("mode") or "").strip().lower() in ("write", "写", "写入") else "read"),
        "output": (data.get("output") or "").strip(),
        "assumptions": [str(a).strip() for a in (data.get("assumptions") or []) if str(a).strip()][:10],
        "questions": questions[:10],
    }


def _mock_agent_reply(conn: str) -> dict[str, Any]:
    # 离线（mock/strict）兜底：直接给一份可部署的模板提案，全流程可走通
    script = (
        '# name: 示例日报\n# cron: 0 9 * * *\n'
        'from lib import query, summary\n'
        '\n'
        'if __name__ == "__main__":\n'
        '    rows = query("SELECT COUNT(*) AS n FROM orders")\n'
        '    summary("查询返回 %d 行" % (rows[0]["n"] if rows else 0))\n'
    )
    nxt = cron.next_after("0 9 * * *", _dt.datetime.now())
    return {
        "ok": True,
        "reply": "Mock 模式下直接给了一份示例脚本（离线演示）。你可以「继续修改」告诉我改什么，或直接部署。",
        "needs": "proposal",
        "proposal": {
            "script": script,
            "name": "示例日报",
            "cron": "0 9 * * *",
            "connection": conn,
            "summary": "每日对 orders 做一次计数查询（Mock 示例，可继续修改）。",
            "next_run": nxt.isoformat() if nxt else None,
        },
    }


@router.post("/agent")
async def task_agent(body: AgentBody) -> StreamingResponse:
    """AI 对话创作任务：SSE 流式。text 块实时推送，done 事件带 {needs, reply, proposal?, plan?}。

    needs 三态：plan（需求登记单待确认）/ proposal（脚本提案）/ clarify（自由澄清）。
    harness 式 ReAct：get_schema + candidate_write/read/test 工具全程常开，
    候选脚本必须通过沙箱自测才允许交付（硬拦截，3 次后放行但标记 untested）。
    交付内容以沙箱验证通过的候选快照为准。SQL 经 /query 过闸门，隐私红线不破。
    """
    import asyncio
    from types import SimpleNamespace

    from app.ai import context as ai_ctx
    from app.ai.gateway import build_provider, is_effective_mock, resolve_provider_cfg

    state = get_state()
    conn_id = (body.connection_id or "").strip()
    last_user = next((m.content for m in reversed(body.messages) if m.role == "user"), "")

    cfg = resolve_provider_cfg(state, SimpleNamespace(model_id=None, reasoning="low"))

    # mock 兜底：离线直接给模板提案
    if is_effective_mock(cfg):
        async def _mock_sse():
            yield _sse_ev("text", "Mock 模式，以下是示例提案脚本：\n\n")
            await asyncio.sleep(0.05)
            yield _sse_ev("text", "```python\nfrom lib import query, summary\n...")
            await asyncio.sleep(0.05)
            yield _sse_ev("done", {
                "needs": "proposal",
                "reply": "Mock 示例，可继续修改后部署。",
                "proposal": _mock_agent_reply(conn_id).get("proposal"),
            })
            yield "data: [DONE]\n\n"
        return StreamingResponse(_mock_sse(), media_type="text/event-stream")

    provider = build_provider(cfg)
    edit_task = (body.edit_task or "").strip()

    async def _stream():
        full_text = ""
        _completed = False
        _interrupt_reason: str | None = None
        ws_state: dict | None = None  # 会话状态（连接解析失败等早退路径下为 None，finally 跳过写回）
        try:
            # 立即发心跳，建立连接（防止代理超时）
            yield _sse_ev("text", "")
            job = None
            f = None
            # 连接归一：body 传 id 或名称均可；编辑模式缺省用任务声明的连接。
            # 解析失败 → 明确报错，绝不静默猜（历史上 id/name 混用曾导致沙箱测错库）。
            conn_key = conn_id
            if edit_task:
                system = _TASK_AGENT_EDIT_PROMPT
                job = state.jobs.get(edit_task)
                if job:
                    f = state.jobs.file(edit_task)
                    try:
                        cur_script = f.read_text(encoding="utf-8", errors="replace") if f else ""
                    except OSError:
                        cur_script = ""
                    system += f"\n\n## 当前脚本（任务名：{edit_task}，cron：{job.cron}，连接：{job.connection}）\n```python\n{cur_script}\n```\n用户要修改这个脚本，请按要求改动后输出完整新脚本。"
                    if not conn_key:
                        conn_key = job.connection
            else:
                system = _TASK_AGENT_PROMPT
            try:
                conn_id_local, conn_label = _resolve_connection(state, conn_key)
            except ValueError as e:
                yield _sse_ev("error", {"message": str(e)})
                yield "data: [DONE]\n\n"
                return
            # schema 组装在流内完成，不阻塞响应头
            if conn_id_local:
                try:
                    schema_text, _ = await ai_ctx.assemble_context_full(state, conn_id_local, None, last_user, skip_retrieval=True)
                    system += f"\n【数据源结构】\n{schema_text}"
                except Exception:
                    pass
            msgs = [{"role": "system", "content": system}] + [
                {"role": m.role, "content": m.content} for m in body.messages
            ]

            # ── harness 式 ReAct Agent：Reason→Act→Observe，工具全程常开，
            # 自测通过才允许交付。护栏只防失控：动作总数 ≤ MAX_ACTIONS、墙钟 ≤ 上限。
            from app.ai.tools import task_dev as _td

            import hashlib
            import time as _time

            sid = (body.session_id or "").strip()
            if not sid:
                sid = "anon_" + hashlib.sha1("|".join(m.content for m in body.messages).encode()).hexdigest()[:12]
            from app.config import get_env as _ge

            ws_dir = _ge().data_dir / "task_ws" / sid
            ws_dir.mkdir(parents=True, exist_ok=True)
            cand = ws_dir / "candidate.py"
            # 编辑模式：候选文件以现有脚本为起点（agent 直接读/改，不用重抄）
            if edit_task and job and not cand.exists():
                try:
                    cur_script = f.read_text(encoding="utf-8", errors="replace") if f else ""
                    cand.write_text(cur_script, encoding="utf-8")
                except OSError:
                    pass

            MAX_ACTIONS = 30
            WALL_CLOCK = 420.0
            _t0 = _time.monotonic()
            _actions = 0
            _consec_writes = 0  # 连续 candidate_write 未测次数（护栏：强制补观察）
            _untested_blocks = 0  # 未测交付拦截次数（3 次后放行，提案标记 untested）
            plan_blocks = 0  # PLAN 硬闸打回次数（本次请求内最多 2 次，之后放行并标记 plan_skipped）
            _last_test_tail = ""  # 最近一次测试输出尾部（中断摘要用）

            # ── 会话状态：跨请求恢复（PLAN 闸门 / 已测快照 / 累计护栏 / 中断摘要）──
            ws_state = _load_ws_state(ws_dir)
            if body.confirm_plan:
                ws_state["plan_confirmed"] = True
            # 启发式自动确认：回放历史里模型出过 PLAN 且用户在其后回复过（打字回答=确认推进），
            # 或已交付过提案（老会话兼容）。前端回放时为 plan/proposal 气泡附加协议标记。
            if not edit_task and not ws_state.get("plan_confirmed"):
                _saw_plan = False
                for m in body.messages:
                    c = m.content or ""
                    if m.role == "assistant" and _REPLAY_PROPOSAL_MARK in c:
                        ws_state["plan_confirmed"] = True
                        break
                    if m.role == "assistant" and _REPLAY_PLAN_MARK in c:
                        _saw_plan = True
                    elif _saw_plan and m.role == "user":
                        ws_state["plan_confirmed"] = True
                        break
            _passed_script: str | None = ws_state.get("passed_script") or None
            cum_actions = int(ws_state.get("cumulative_actions") or 0)
            cum_clock = float(ws_state.get("wall_clock_used") or 0.0)
            _interrupt_reason: str | None = None
            _completed = False

            # 中断摘要：上一轮断点续跑（一次性注入，用后即清）
            interrupt = ws_state.pop("last_interrupt", None)
            if interrupt:
                _parts = [f"⚠ 上一轮创作在此中断（原因：{interrupt.get('reason') or '未知'}；"
                          f"已完成动作 {interrupt.get('actions_done') or 0} 个）。"]
                if interrupt.get("test_tail"):
                    _parts.append(f"候选脚本最近一次测试输出尾部：\n{interrupt['test_tail']}\n")
                _parts.append("请从中断处继续：先 candidate_read 查看当前候选（若存在），不要从头重写。")
                yield _sse_ev("text", "\n> ⏱ 已从上次中断处恢复会话进度。\n\n")
                msgs.append({"role": "user", "content": "\n".join(_parts)})

            while True:
                tool_calls: list = []
                turn_text = ""  # 本轮 assistant 文本（回灌消息序列用）
                async for chunk in provider.chat_stream(msgs, _TASK_TOOLS, ctx={
                    "conn_id": conn_id_local or None, "connection": conn_label, "skill": "task-agent",
                    "source": "egress", "status": "egress",
                }):
                    reason = getattr(chunk, "reasoning", None)
                    if reason:
                        yield _sse_ev("reasoning", reason)
                    delta = getattr(chunk, "delta", None) or getattr(chunk, "content", None) or ""
                    if delta:
                        turn_text += delta
                        full_text += delta
                        yield _sse_ev("text", delta)
                    tc = getattr(chunk, "tool_calls", None)
                    if tc:
                        tool_calls = list(tc)

                # 无工具调用 = agent 决定交付（PLAN / 提案 / 澄清）
                if not tool_calls:
                    # PLAN 硬闸（仅新建模式）：需求未经确认 → 打回出 PLAN；2 次后放行并标记 plan_skipped
                    if _parse_proposal(full_text) and not edit_task \
                            and not ws_state.get("plan_confirmed") and plan_blocks < 2:
                        plan_blocks += 1
                        msgs.append({
                            "role": "user",
                            "content": "⚠ 需求尚未确认，不能交付提案（平台强制：先对齐再动手）。"
                                       "请先输出 ---PLAN--- 需求登记单（cron/连接/表/读写/产出/假设/问题），"
                                       "等用户确认或回答问题后再进入写脚本流程。",
                        })
                        continue
                    # 未测交付硬拦截：打回重测；3 次后放行并标记 untested
                    if _parse_proposal(full_text) and _passed_script is None and _untested_blocks < 3:
                        _untested_blocks += 1
                        msgs.append({
                            "role": "user",
                            "content": "⚠ 提案脚本尚未在沙箱通过 candidate_test，不能交付。"
                                       "请先 candidate_write 写入候选（若还没写），再 candidate_test 验证；"
                                       "报错就修到通过再交付。不要交付没验证过的脚本。",
                        })
                        continue
                    break

                _actions += len(tool_calls)
                if _actions > MAX_ACTIONS or (_time.monotonic() - _t0) > WALL_CLOCK \
                        or (cum_actions + _actions) > _SESSION_MAX_ACTIONS \
                        or (cum_clock + (_time.monotonic() - _t0)) > _SESSION_WALL_CLOCK:
                    _interrupt_reason = "达到护栏上限（动作/墙钟预算耗尽）"
                    yield _sse_ev("text", "\n> ⛔ 调试动作已达防护上限，停止循环，交出现有状态。\n\n")
                    break

                # OpenAI 兼容铁律：tool 消息前必须有带 tool_calls 的 assistant 消息
                msgs.append({
                    "role": "assistant",
                    "content": turn_text or None,
                    "tool_calls": [
                        {"id": t.id, "type": "function",
                         "function": {"name": t.name, "arguments": json.dumps(t.arguments, ensure_ascii=False)}}
                        for t in tool_calls
                    ],
                })

                _tested_ok = False
                for tc in tool_calls:
                    label = {
                        "candidate_write": "写入候选脚本",
                        "candidate_read": "读取候选脚本",
                        "candidate_test": "自测候选脚本（沙箱）",
                        "get_schema": "查看表结构",
                    }.get(tc.name, tc.name)
                    yield _sse_ev("text", f"\n> 🔧 {label}…\n\n")
                    _tok = _td.set_workspace(str(ws_dir))
                    try:
                        outcome = await execute_tool(state, tc.name, tc.arguments, conn_id_local)
                        content = json.dumps(outcome.result, ensure_ascii=False)
                    except Exception as e:  # noqa: BLE001
                        content = json.dumps({"ok": False, "error": str(e)[:300]}, ensure_ascii=False)
                    finally:
                        _td.reset_workspace(_tok)

                    # 自测结果实时流给用户看（agent 的调试足迹可见）
                    if tc.name == "candidate_test":
                        try:
                            _d = json.loads(content)
                            if _d.get("ok"):
                                _tested_ok = True
                                try:
                                    _passed_script = cand.read_text(encoding="utf-8", errors="replace")
                                except OSError:
                                    _passed_script = None
                                _untested_blocks = 0
                                ws_state["passed_script"] = _passed_script  # 跨请求保留：已测基线
                                _last_test_tail = ""
                                yield _sse_ev("text", f"\n> ✅ 自测通过：{_d.get('summary', 'OK')} —— 请直接交付提案。\n\n")
                            else:
                                _lines = [l for l in (str(_d.get("output") or "")).splitlines() if l.strip()]
                                _hint = _lines[-1][:160] if _lines else (str(_d.get("summary") or (str(_d.get("error") or "失败")))[:160])
                                _last_test_tail = (str(_d.get("output") or ""))[-500:]
                                yield _sse_ev("text", f"\n> ❌ 自测失败（{_d.get('status')}）：{_hint}\n\n")
                        except Exception:
                            yield _sse_ev("text", "\n> ✅ 自测完成\n\n")

                    if tc.name == "candidate_write":
                        _consec_writes += 1
                    else:
                        _consec_writes = 0

                    # 回灌给模型的结果截断，防上下文爆炸（测试日志可能很长），但保持诊断信息够用
                    msgs.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": content[:4000]})

                # user 注入必须在工具批次之后（不能插进 tool 消息序列中间）
                if _tested_ok:
                    msgs.append({
                        "role": "user",
                        "content": "✅ 候选脚本已通过沙箱测试。现在交付：把候选脚本原样填入下面 JSON 的 script 字段输出 ---PROPOSAL---（不要再调用任何工具）：\n"
                                   f'{"```python\n" + (_passed_script or "") + "\n```" if _passed_script else ""}',
                    })
                elif _consec_writes >= 3:
                    msgs.append({
                        "role": "user",
                        "content": "⚠ 你已经连续多次写候选但从未看到测试结果，这样改不动脚本。请立即调用 candidate_test 运行当前候选，读取 stdout/报错，再针对性地改。",
                    })
                    _consec_writes = 0

            proposal = _parse_proposal(full_text)
            plan = _parse_plan(full_text)
            # 交付内容以通过测试的候选为准：模型抄写的 script 与快照不一致时强制替换（绝不放行未验证代码）
            untested = False
            if proposal and _passed_script is not None and (proposal.get("script") or "").strip() != _passed_script.strip():
                proposal["script"] = _passed_script
                yield _sse_ev("text", "\n> 🔒 提案脚本已替换为沙箱验证通过的候选版本。\n\n")
            if proposal and _passed_script is None:
                untested = True  # 拦截 3 次后放行的未验证提案 → 前端警告展示
            if plan and not proposal:
                reply = re.sub(r"---PLAN---.*$", "", full_text, flags=re.S).strip()
            elif proposal:
                reply = re.sub(r"---PROPOSAL---.*$", "", full_text, flags=re.S).strip()
            else:
                reply = full_text

            # 兜底：模型没吐提案但候选已通过测试 → 后端用通过的候选快照构建提案（绝不丢已验证成果）
            # PLAN 硬闸同样约束兜底路径：需求未确认（且非编辑模式）时不自动提案
            if proposal is None and plan is None and _passed_script \
                    and (ws_state.get("plan_confirmed") or edit_task):
                from app.tasks.jobs import parse_header as _ph

                _hdr = _ph(_passed_script)
                _cron_s = (_hdr.get("cron") or "").strip() or (job.cron if edit_task and job else "")
                _name_s = (_hdr.get("name") or "").strip() or edit_task or "任务"
                _conn_s = (_hdr.get("connection") or "").strip() or conn_label
                _nxt = cron.next_after(_cron_s, _dt.datetime.now()) if _cron_s else None
                if _nxt:
                    proposal = {
                        "script": _passed_script,
                        "name": _name_s,
                        "cron": _cron_s,
                        "connection": _conn_s,
                        "summary": "脚本已通过隔离沙箱验证，按验证通过的版本交付。",
                        "next_run": _nxt.isoformat(),
                    }
                    yield _sse_ev("text", "\n> 📦 已按验证通过的候选脚本自动生成提案。\n\n")
                else:
                    # 候选没有合法 cron 声明 → 不硬造调度，走澄清让用户补
                    reply = reply or _passed_script[:200] + "\n\n脚本已通过沙箱验证，但缺少合法 cron 声明，请在顶部字段补充调度时间。"

            if proposal:
                proposal["untested"] = untested
                if plan_blocks >= 2 and not edit_task and not ws_state.get("plan_confirmed"):
                    proposal["plan_skipped"] = True  # PLAN 硬闸 2 次打回后仍交付 → 前端黄标提示
            needs = "proposal" if proposal else ("plan" if plan else "clarify")
            yield _sse_ev("done", {"needs": needs, "reply": reply, "proposal": proposal, "plan": plan if needs == "plan" else None})
            _completed = True
        except Exception as e:
            _interrupt_reason = f"error: {str(e)[:200]}"
            yield _sse_ev("error", {"message": str(e)})
        finally:
            # 会话状态写回：累计护栏、已测快照、中断摘要（含客户端断开/超时——GeneratorExit 也会走 finally）
            if ws_state is not None:
                if not _completed and not _interrupt_reason:
                    _interrupt_reason = "客户端断开或请求超时"
                ws_state["cumulative_actions"] = cum_actions + _actions
                ws_state["wall_clock_used"] = round(cum_clock + (_time.monotonic() - _t0), 1)
                ws_state["passed_script"] = _passed_script
                if _interrupt_reason and not _completed:
                    ws_state["last_interrupt"] = {
                        "reason": _interrupt_reason,
                        "actions_done": _actions,
                        "test_tail": _last_test_tail,
                    }
                _save_ws_state(ws_dir, ws_state)
        yield "data: [DONE]\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


def _sse_ev(evt_type: str, data) -> str:
    """构造单条 SSE data 行。"""
    payload = {"type": evt_type, **(data if isinstance(data, dict) else {"content": data})}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"