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

# 任务创作会话的工具白名单（ReAct 简版 agent）：
# get_schema（查表结构）+ candidate_write/read/test（候选工作区：写→读→沙箱自测）。
# 写盘唯一路径仍是 deploy（人确认）；测试跑双重沙箱物理隔离。
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
    """新任务落盘：AI 对话产出的脚本写入 jobs/。产物永远是一个 .py。"""
    state = get_state()
    name = (body.name or "").strip()
    cron_s = (body.cron or "").strip()
    if not name or not cron_s:
        raise HTTPException(status_code=400, detail="任务名与 cron 必填")
    if cron.next_after(cron_s, _dt.datetime.now()) is None:
        raise HTTPException(status_code=400, detail=f"无效 cron: {cron_s}")

    target = state.jobs.dir / _slug(name)
    if target.exists() and not body.overwrite:
        raise HTTPException(status_code=409, detail=f"同名脚本已存在（{target.name}），如需覆盖请带 overwrite=true")

    text = (body.script or "").strip()
    if not text:
        text = 'from lib import query, summary\n\nif __name__ == "__main__":\n    summary("TODO: 填写查询逻辑")\n'

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
        prepend.append("# enabled: true")
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
    return {"ok": True, "task": _job_dict(state, job)}


@router.get("/{name}/runs")
async def task_runs(name: str) -> dict[str, Any]:
    state = get_state()
    if not state.jobs.has(name):
        raise HTTPException(status_code=404, detail=f"任务 {name} 不存在")
    return {"ok": True, "name": name, "runs": state.job_store.recent_runs(name)}


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
    """在隔离沙箱中执行脚本一次并返回结果（不保存任务、不改任何真实文件）。"""
    from app.tasks.runner import run_job_raw

    script = body.get("script", "")
    connection_id = body.get("connection_id", "")
    if not script.strip():
        raise HTTPException(status_code=400, detail="script 不能为空")
    state = get_state()
    result = await run_job_raw(state, script, connection_id, timeout=30)
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
    """整文件保存（AI 修订/人工编辑后）。若头被误删，回写 name/cron 兜底再注册。"""
    state = get_state()
    job = state.jobs.get(name)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务 {name} 不存在")
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

_TASK_AGENT_PROMPT = """你是 TableTalk 的定时任务脚本创作助手。用户想要一个定时任务，你的成果是一个可直接部署的 Python 脚本。
任务 = 一个 .py 文件。你只负责写脚本内容——部署、调度、落盘都是平台的职责，你不需要也不应该访问文件系统。

## 平台 SDK（脚本唯一可依赖的库，仅这些函数）
- `query("SELECT ...")` → list[dict]（如 [{"n": 123}]）；查询失败会抛异常
- `insert("表名", ["列1","列2"], [[v1,v2], ...])` → 写库（规避危险 DML，走 scheduled INSERT 放行）
- `write("UPDATE ...")` → 有 WHERE 的 UPDATE/DELETE（会被审查，无 WHERE 必失败）
- `summary("一句话结果")` → 打印结果摘要，会登记为该次运行的执行摘要
- 标准库随意；**绝不 import 平台外模块**（如 requests/pandas/sqlalchemy）

## 硬性约束
1. **禁止 DDL**：绝不生成 CREATE/ALTER/DROP/TRUNCATE。若目标表不存在，脚本应 query 探测后 `summary("目标表 xxx 不存在，请先手动建表后重新部署")` 并退出。
2. 语法正确、缩进 4 空格；必须有 `if __name__ == "__main__": main()` 入口，main() 末尾必须 `summary(...)`。
3. 幂等：重复执行不产生重复/脏数据（如先查当天是否已写入，存在则跳过空跑）。
4. 只读统计优先（零风险）——只有用户明确要写数据才用 insert/write。

## 工具（候选工作区）
你有一个**独立候选文件**保存你的当前最佳脚本，全程可写/读/测，隔离在沙箱、绝不触碰真实文件：
- `candidate_write(script)`：把当前完整脚本写入候选文件（每次都整套覆盖）。
- `candidate_read()`：读回候选文件当前内容。改复杂脚本前先读，避免漂移、避免重复抄错。
- `candidate_test()`：在隔离沙箱运行候选文件，返回 stdout/摘要/报错/退出码——SQL 照常过闸门。
- `get_schema`：实时查看表结构（传表名看列，不传看全部表）。不确定表名/列名/类型时先查，别凭空猜；已有【数据源结构】就不用重复调。

## 沙箱与生产的铁律
- **沙箱执行环境与生产完全一致**（同一 lib SDK，query/summary/write/insert 都可用）。
- 因此看到 `NameError: name 'query' is not defined` 或 `'summary' is not defined` → **100% 是你脚本漏了 `from lib import query, summary`**，绝不是沙箱的错。修导入，不要怀疑环境。
- 沙箱里能跑的，生产必能跑；沙箱里报的错，就是生产会报的错。

## 工作方式（ReAct 调试循环）
- 你是一个自驱调试的 agent：**Reason → Act → Observe 循环**，直到脚本通过验证。
- 典型循环：写候选 → `candidate_test` → 看报错 → 改（必要时先 `candidate_read` 看清现状）→ 再测 → …直到 `ok=true`。
- **节奏铁律：每写一版，立刻测一版。没有测试结果前，禁止重写第二版。** 测试结果（尤其报错）是你唯一的"观察"，靠它决定改什么。
- **由你自己决定何时完成**：`candidate_test` 通过（ok=true）就**立即把候选脚本作为提案交付**（下一条就是 ---PROPOSAL---，不许再 candidate_read/candidate_write），不再抛光。
- 别急着交付带错的脚本：报错→修→再测，这是你的任务本分；也不要为了"完美"无限打磨，通过即为合格。
- 极简需求（如一条只读统计）可以跳过调试直接产出，别为简单事过度造作。

## 交付
- 需求足够明确（脚本已通过验证）就产出。格式——只输出一行标记 + JSON，JSON 里 script 为完整可运行脚本：
---PROPOSAL---
{"script": "<完整 Python 脚本>", "name": "任务名", "cron": "0 9 * * *", "connection": "连接名", "summary": "一句话说明这脚本干嘛"}

cron 规则：5 字段（分 时 日 月 周，空格分隔）。`0 9 * * *`=每天9点、`*/30 * * * *`=每30分钟、`0 9 * * 1`=每周一9点。
名称用中文短语，≤20 字；连接名必须与数据源结构一致。
"""

_TASK_AGENT_EDIT_PROMPT = """你是 TableTalk 的定时任务脚本编辑助手。用户要修改一个已有任务脚本。
**当前脚本全文已注入下方代码块**——内容已经在这里了，你不需要去文件系统找它，也绝不能访问文件。平台负责把你说改的结果落盘。

## 改法
- **只改用户点名要改的地方**，其余保持原样（一字不改）。
- 改完输出**完整新脚本**（含声明头），不是补丁。
- 用户若同时改了名称/cron 字段（对话框顶部），会作为附加要求给出；以用户本次对话要求为准。

## 产出格式（需求明确就直接给）
---PROPOSAL---
{"script": "<完整修改后的脚本（含 # name / # cron / # connection / # enabled 声明头）>", "name": "任务名", "cron": "0 9 * * *", "connection": "连接名", "summary": "一句话说明改了什么"}

## 硬性约束
1. 禁止 DDL（CREATE/ALTER/DROP/TRUNCATE）。
2. 语法正确、缩进 4 空格、只用平台 SDK（query/insert/write/summary）+ 标准库。
3. 保留入口结构 `if __name__ == "__main__": main()` 与 main() 末尾的 `summary(...)`。
4. 声明头必须完整（# name / # cron / # connection / # enabled）。
5. 需求含糊需要澄清时：一次最多 1 个问题，直接问；不得擅自改没被要求的东西。
6. 不确定表结构可用 `get_schema` 查证；候选文件可 candidate_read 读回、candidate_write 改写、candidate_test 沙箱自测（不会改真实文件）。
7. 你先 candidate_write 把改动落进候选文件，再 candidate_test 验证；报错→修→再测直到通过，然后交付提案。
"""


class AgentMessage(BaseModel):
    role: str
    content: str


class AgentBody(BaseModel):
    messages: list[AgentMessage]
    connection_id: str = ""
    edit_task: str = ""
    session_id: str = ""  # 任务创作会话 id（前端每次打开对话框生成一次）；用于候选工作区隔离与跨轮复用


def _parse_proposal(text: str) -> dict[str, Any] | None:
    m = re.search(r"---PROPOSAL---\s*(.*)$", text, re.S)
    if not m:
        return None
    chunk = m.group(1).strip()
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
    if not isinstance(data, dict) or not data.get("script"):
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
    """AI 对话创作任务：SSE 流式。text 块实时推送，done 事件带提案或澄清。
    与主页 Agent 共享底层：chat_stream + assemble_context_full + 中央记账（llm_log/cost/egress）。
    仅暴露 get_schema 一个只读工具（写脚本时实时查表结构），不接 run_query/run_dml —— 隐私红线不破。
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
        try:
            # 立即发心跳，建立连接（防止代理超时）
            yield _sse_ev("text", "")
            # 选择系统提示：编辑模式注入当前脚本
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
                    if not conn_id and job.connection:
                        conn_id_local = job.connection
                    else:
                        conn_id_local = conn_id
                else:
                    conn_id_local = conn_id
            else:
                system = _TASK_AGENT_PROMPT
                conn_id_local = conn_id
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
            conn_label = ""
            try:
                conn_label = state.connections.get(conn_id_local).name if conn_id_local else ""
            except Exception:
                pass

            # ── ReAct 简版 agent：Reason→Act→Observe，直到 agent 自己决定交付（无工具调用=终态）。
            # 护栏只防死循环：动作总数 ≤ MAX_ACTIONS、墙钟 ≤ 上限；不作"计划"。
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
            _delivery = False   # 候选已通过测试 → 下一轮禁用全部工具，逼交付提案
            _passed_script: str | None = None  # 通过瞬间的候选快照（后端兜底交付用）
            _tool_names = {t["function"]["name"] for t in _TASK_TOOLS}
            _untested_warned = False  # 未测提案只提醒一次
            while True:
                tool_calls: list = []
                # 已通过：不给工具，模型只能输出终态（提案/澄清）
                async for chunk in provider.chat_stream(msgs, [] if _delivery else _TASK_TOOLS, ctx={
                    "conn_id": conn_id_local or None, "connection": conn_label, "skill": "task-agent",
                    "source": "egress", "status": "egress",
                }):
                    reason = getattr(chunk, "reasoning", None)
                    if reason:
                        yield _sse_ev("reasoning", reason)
                    delta = getattr(chunk, "delta", None)
                    if delta:
                        full_text += delta
                        yield _sse_ev("text", delta)
                    tc = getattr(chunk, "tool_calls", None)
                    if tc:
                        # delivery 模式硬清空（防幻想起始工具）；否则只放行白名单
                        tool_calls = [] if _delivery else [t for t in tc if t.name in _tool_names]
                # 无工具调用 = agent 决定交付（提案或澄清）
                if not tool_calls:
                    # 把关：吐了提案但从未通过测试 → 提醒一次先自测（已提醒过或通过了才放行）
                    if _parse_proposal(full_text) and not _delivery and not _untested_warned:
                        _untested_warned = True
                        msgs.append({
                            "role": "user",
                            "content": "⚠ 你交付的提案脚本尚未在沙箱通过 candidate_test。请先 candidate_write 写入候选（若还没写），再 candidate_test 验证；报错就修到通过再交付。不要交付没验证过的脚本。",
                        })
                        continue
                    break
                _actions += len(tool_calls)
                if _actions > MAX_ACTIONS or (_time.monotonic() - _t0) > WALL_CLOCK:
                    yield _sse_ev("text", "\n> ⛔ 调试动作已达防护上限，停止循环，交出现有状态。\n\n")
                    break
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
                        _delivery = False
                        try:
                            _d = json.loads(content)
                            if _d.get("ok"):
                                # 通过瞬间快照候选（防后续改写漂移）；下一轮没收工具逼交付
                                try:
                                    _passed_script = cand.read_text(encoding="utf-8", errors="replace")
                                except OSError:
                                    _passed_script = None
                                _delivery = True
                                yield _sse_ev("text", f"\n> ✅ 自测通过：{_d.get('summary', 'OK')} —— 请直接交付提案。\n\n")
                                msgs.append({
                                    "role": "user",
                                    "content": "✅ 候选脚本已通过沙箱测试。现在交付：把候选脚本原样填入下面 JSON 的 script 字段输出 ---PROPOSAL---（不要再调用任何工具）：\n"
                                    f'{"```python\n" + _passed_script + "\n```" if _passed_script else ""}',
                                })
                            else:
                                _lines = [l for l in (str(_d.get("output") or "")).splitlines() if l.strip()]
                                _hint = _lines[-1][:120] if _lines else (str(_d.get("summary") or (str(_d.get("error") or "失败")))[:120])
                                yield _sse_ev("text", f"\n> ❌ 自测失败（{_d.get('status')}）：{_hint}\n\n")
                        except Exception:
                            yield _sse_ev("text", "\n> ✅ 自测完成\n\n")
                    # 护栏：连续写 3 次未测 → 注入强制"先观察"提醒（不给它盲写的余地）
                    if tc.name == "candidate_write":
                        _consec_writes += 1
                        if _consec_writes >= 3:
                            msgs.append({
                                "role": "user",
                                "content": "⚠ 你已经连续多次写候选但从未看到测试结果，这样改不脚本。请立即调用 candidate_test 运行当前候选，读取 stdout/报错，再针对性地改。",
                            })
                            _consec_writes = 0
                    else:
                        _consec_writes = 0
                    # 回灌给模型的结果截断，防上下文爆炸（测试日志可能很长），但保持诊断信息够用
                    msgs.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": content[:4000]})
            proposal = _parse_proposal(full_text)
            reply = re.sub(r"---PROPOSAL---.*$", "", full_text, flags=re.S).strip() if proposal else full_text
            # 兜底：模型没吐提案但候选已通过测试 → 后端用通过的候选快照构建提案（绝不丢已验证成果）
            if proposal is None and _passed_script:
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
                else:
                    # 候选没有合法 cron 声明 → 不硬造调度，走澄清让用户补
                    reply = reply or _passed_script[:200] + "\n\n脚本已通过沙箱验证，但缺少合法 cron 声明，请在顶部字段补充调度时间。"
                yield _sse_ev("text", "\n> 📦 已按验证通过的候选脚本自动生成提案。\n\n")
            yield _sse_ev("done", {"needs": "proposal" if proposal else "clarify", "reply": reply, "proposal": proposal})
        except Exception as e:
            yield _sse_ev("error", {"message": str(e)})
        yield "data: [DONE]\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


def _sse_ev(evt_type: str, data) -> str:
    """构造单条 SSE data 行。"""
    payload = {"type": evt_type, **(data if isinstance(data, dict) else {"content": data})}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"