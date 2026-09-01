"""脚本任务注册表单测：声明头驱动、扫描派生、删文件即消失、头改写。"""
from __future__ import annotations

from pathlib import Path

from app.tasks.jobs import JobRegistry, parse_header, rewrite_header


def _seed(d: Path) -> Path:
    jobs = d / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    (jobs / "lib.py").write_text("def query(): ...\n")  # 无声明 → 辅助模块
    (jobs / "a_daily.py").write_text(
        "# name: 每日销售日报\n# cron: 0 9 * * *\n# connection: demo\n# enabled: true\nprint('hi')\n"
    )
    (jobs / "b_bad.py").write_text("# name: 坏任务\n# cron: not-cron\nx=1\n")
    (jobs / "c_off.py").write_text("# name: 停用任务\n# cron: 0 8 * * *\n# enabled: false\nx=1\n")
    return jobs


def test_scan_declaration_driven(tmp_path):
    _seed(tmp_path)
    reg = JobRegistry(tmp_path)
    names = sorted(j.name for j in reg.list())
    assert names == ["停用任务", "每日销售日报"]
    a = reg.get("每日销售日报")
    assert a and a.connection == "demo" and a.enabled and not a.system and a.file == "a_daily.py"
    off = reg.get("停用任务")
    assert off and not off.enabled
    assert reg.get("坏任务") is None  # 非法 cron


def test_delete_file_job_disappears(tmp_path):
    jobs = _seed(tmp_path)
    reg = JobRegistry(tmp_path)
    assert reg.get("每日销售日报")
    (jobs / "a_daily.py").unlink()
    reg.reload()
    assert reg.get("每日销售日报") is None
    assert len(reg.list()) == 1  # 只剩停用任务


def test_system_flag(tmp_path):
    jobs = _seed(tmp_path)
    (jobs / "s_raw.py").write_text("# name: 日志保留\n# cron: 0 3 * * *\n# system: true\nx=1\n")
    reg = JobRegistry(tmp_path)
    s = reg.get("日志保留")
    assert s and s.system


def test_parse_header():
    h = parse_header("# name: X\n# cron: 0 9 * * *\n# connection: demo\nprint(1)")
    assert h == {"name": "X", "cron": "0 9 * * *", "connection": "demo"}


def test_rewrite_header_changes_only_existing_keys(tmp_path):
    jobs = _seed(tmp_path)
    f = jobs / "a_daily.py"
    rewrite_header(f, {"enabled": "false", "cron": "0 7 * * *", "not-a-key": "x"})
    text = f.read_text(encoding="utf-8")
    assert "# enabled: false" in text
    assert "# cron: 0 7 * * *" in text
    assert "# not-a-key" not in text
    # 没动正文
    assert "print('hi')" in text