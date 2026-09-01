"""脚本任务执行器：子进程跑 jobs/<file>，env 注入 sidecar 地址与 token。

脚本经 jobs/lib.py（SDK）走后端 API → 过闸门 → 记审计。进程边界是"删了没影响"的物理来源：
平台从不 import 脚本，脚本够不到应用进程内存/直连凭据。
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.state import AppState

JOB_TIMEOUT_DEFAULT = 300
OUTPUT_CAP = 8192
_running: dict[str, asyncio.subprocess.Process] = {}  # 在飞子进程（供取消）


def _extract_summary(text: str) -> str | None:
    last = None
    for line in text.splitlines():
        if line.startswith("TT-SUMMARY: "):
            last = line[len("TT-SUMMARY: "):].strip()
    return last or None


async def run_job(state: "AppState", job_name: str, trigger: str = "schedule") -> dict:
    """执行一个脚本任务（到点触发与手动 Run Now 共用，不构成旁路）。"""
    from app.config import get_env, get_token

    if job_name in _running:
        return {"ok": False, "error": f"任务 {job_name} 正在运行中"}

    from app.tasks.store import JobStore

    job = state.jobs.get(job_name)
    if not job:
        return {"ok": False, "error": f"任务 {job_name} 不存在"}
    script = state.jobs.file(job_name)
    if not script or not script.exists():
        return {"ok": False, "error": f"任务 {job_name} 脚本缺失"}

    store = JobStore(get_env().data_dir)
    run_id = store.start_run(job_name)

    base_url = f"http://127.0.0.1:{get_env().port}"
    env = {
        **os.environ,
        "TABLETALK_URL": base_url,
        "TABLETALK_TOKEN": get_token() or "",
        "TABLETALK_JOB": job_name,
        "TABLETALK_JOB_CONNECTION": job.connection or "",
    }
    timeout = int(os.environ.get("TABLETALK_JOB_TIMEOUT", str(JOB_TIMEOUT_DEFAULT)))

    proc = await asyncio.create_subprocess_exec(
        sys.executable, script.name,
        cwd=str(state.jobs.dir),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _running[job_name] = proc

    # 超时：杀了进程并记 timeout
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            pass
        status = "timeout"
        summary = f"执行超时（{timeout}s）"
        output = f"[TIMEOUT {timeout}s]\n"
        store.finish_run(run_id, status, summary, output)
        _audit(state, job, status, summary)
        return {"ok": False, "status": status, "summary": summary, "trigger": trigger}
    finally:
        _running.pop(job_name, None)

    rc = proc.returncode
    text = (out or b"").decode("utf-8", errors="replace")
    if err:
        text += "\n[stderr]\n" + err.decode("utf-8", errors="replace")
    output = text[:OUTPUT_CAP]

    status = "success" if rc == 0 else "error"
    summary = _extract_summary(text) or (f"exit={rc}" if rc != 0 else "完成")
    store.finish_run(run_id, status, summary, output)
    _audit(state, job, status, summary)
    return {"ok": rc == 0, "status": status, "summary": summary, "exit_code": rc, "trigger": trigger}


def _audit(state: "AppState", job, status: str, summary: str) -> None:
    try:
        state.audit.log(
            connection=job.connection or job.name,
            origin="scheduled",
            tier="job",
            verdict="executed" if status == "success" else "block",
            status=f"scheduled_{status}",
            sql=f"[scheduled] {job.name}",
            source="scheduled",
        )
    except Exception:  # noqa: BLE001 - 审计失败不影响任务本身
        pass


def cancel_job(name: str) -> bool:
    """取消正在运行的子进程。返回 True 表示找到并 kill。"""
    proc = _running.get(name)
    if proc and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return True
    return False


def is_running(name: str) -> bool:
    proc = _running.get(name)
    return proc is not None and proc.returncode is None


def running_jobs() -> list[str]:
    return [k for k, v in _running.items() if v.returncode is None]


async def run_job_raw(state: "AppState", script_text: str, connection_id: str, timeout: int = 30) -> dict:
    """在隔离沙箱中执行一段候选脚本（测试/preview 用），不写 store、不写任务审计。

    双重隔离，保证候选脚本**物理上改不到任何真实文件**：
    1. 副本隔离：沙箱 = jobs/ 目录的一次性副本（lib.py SDK + 辅助模块），cwd 在副本里；
    2. OS 沙箱（macOS sandbox-exec）：禁沙箱目录外的任何文件写/删，网络仅放行本机回环
       （lib.py 经真 API 过闸门所以代表性测试成立）。跑完整个副本销毁。
    """
    import shutil
    import tempfile
    from pathlib import Path

    from app.config import get_env, get_token

    base_url = f"http://127.0.0.1:{get_env().port}"
    env = {
        **os.environ,
        "TABLETALK_URL": base_url,
        "TABLETALK_TOKEN": get_token() or "",
        "TABLETALK_JOB": "_test",
        "TABLETALK_JOB_CONNECTION": connection_id,
    }
    job_name = "_test_sandbox"
    if job_name in _running:
        return {"ok": False, "error": "测试脚本正在运行中"}

    sandbox = Path(tempfile.mkdtemp(prefix="tt-sandbox-"))
    try:
        # 复制 jobs 目录全部 .py（lib.py SDK + 辅助模块）到沙箱副本；reports 等子目录一并
        for f in state.jobs.dir.iterdir():
            if f.is_dir() and f.name == "__pycache__":
                continue
            dst = sandbox / f.name
            if f.is_dir():
                shutil.copytree(f, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(f, dst)
        target = sandbox / "_candidate.py"
        target.write_text(script_text, encoding="utf-8")

        resolved = str(sandbox.resolve())
        profile = sandbox / "sandbox.sb"
        profile.write_text(
            "(version 1)\n"
            "(deny default)\n"
            '(import "system.sb")\n'
            "(allow process*)\n"
            "(allow file-read*)\n"
            f'(allow file-write* (subpath "{resolved}"))\n'
            '(allow network-outbound (remote tcp "localhost:*"))\n'
            "(allow network-inbound)\n"
            "(allow sysctl-read)\n",
            encoding="utf-8",
        )

        # OS 沙箱外层再包 sandbox-exec：禁绝对路径写真实目录，网络仅回环
        argv: list[str] = []
        if sys.platform == "darwin" and shutil.which("sandbox-exec"):
            argv += ["sandbox-exec", "-f", str(profile)]
        argv += [sys.executable, target.name]

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(sandbox),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _running[job_name] = proc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
            return {"ok": False, "status": "timeout", "summary": f"执行超时（{timeout}s）", "output": "", "exit_code": None}
        finally:
            _running.pop(job_name, None)

        rc = proc.returncode
        text = (out or b"").decode("utf-8", errors="replace")
        if err:
            text += "\n[stderr]\n" + err.decode("utf-8", errors="replace")
        summary = _extract_summary(text) or (f"exit={rc}" if rc != 0 else "完成")
        return {"ok": rc == 0, "status": "success" if rc == 0 else "error", "summary": summary, "output": text[:OUTPUT_CAP], "exit_code": rc}
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)