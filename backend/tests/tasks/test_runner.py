"""脚本任务 runner + seed 播种 单测。"""
from __future__ import annotations

from pathlib import Path

from app.tasks import runner
from app.tasks.seed import seed_jobs_dir


def _write(tmp_path, name: str, body: str) -> Path:
    jobs = Path(tmp_path) / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    (jobs / name).write_text(body, encoding="utf-8")
    return jobs


async def test_run_job_success(app_state, tmp_path):
    _write(app_state.env.data_dir, "hello.py", '# name: hello\n# cron: * * * * *\nprint("TT-SUMMARY: world ok")')
    app_state.jobs.reload()
    res = await runner.run_job(app_state, "hello")
    assert res["ok"] is True and res["status"] == "success"
    assert res["summary"] == "world ok"
    last = app_state.job_store.last("hello")
    assert last and last["status"] == "success" and last["summary"] == "world ok"


async def test_run_job_error_captures_traceback(app_state, tmp_path):
    _write(app_state.env.data_dir, "boom.py", '# name: boom\n# cron: * * * * *\nraise RuntimeError("kaboom")')
    app_state.jobs.reload()
    res = await runner.run_job(app_state, "boom")
    assert res["ok"] is False and res["status"] == "error"
    last = app_state.job_store.last("boom")
    assert last["status"] == "error"
    assert "Traceback" in last["output"] and "kaboom" in last["output"]


async def test_run_job_missing_job(app_state):
    res = await runner.run_job(app_state, "不存在")
    assert res["ok"] is False and "不存在" in str(res.get("error"))


def test_seed_writes_lib_and_retention(tmp_path):
    jobs = seed_jobs_dir(tmp_path)
    assert (jobs / "lib.py").exists()
    assert (jobs / "_system_log_retention.py").exists()
    # 幂等：重复播种不覆盖（用户自定义改动保留）
    (jobs / "lib.py").write_text("# user-edit")
    seed_jobs_dir(tmp_path)
    assert (jobs / "lib.py").read_text(encoding="utf-8") == "# user-edit"
    # lib.py 无 name/cron 声明 → 不是任务；retention 脚本是任务
    from app.tasks.jobs import JobRegistry

    reg = JobRegistry(tmp_path)
    assert reg.get("日志保留清理") is not None
    assert len([j for j in reg.list()]) == 1