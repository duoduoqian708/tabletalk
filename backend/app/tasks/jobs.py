"""脚本任务注册表：扫描 data_dir/jobs/*.py，声明头驱动注册。

声明头（文件前 40 行内的 `# key: value`）：
  # name: <任务名>     必填 —— 无 name 或 cron 的文件=辅助模块（lib.py 等），仅可 import，不是任务
  # cron: <5字段>      必填 —— 非法 cron 视为非任务
  # connection: <名>   可选 —— SDK 解析连接名
  # enabled: true|false 可选，默认 true
  # system: true       可选 —— 平台内置脚本（可编辑，不特殊保护）
注册 = 扫描派生；文件消失 = 任务消失，平台零依赖。
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path

from app.tasks.cron import next_after

_HEADER_KEYS = ("name", "cron", "connection", "enabled", "system")
_HEAD_RE = re.compile(r"#\s*(" + "|".join(_HEADER_KEYS) + r")\s*:\s*(.*)$")


@dataclass
class Job:
    name: str
    cron: str
    connection: str = ""
    enabled: bool = True
    system: bool = False
    file: str = ""  # 文件名（jobs 目录下）


def parse_header(text: str) -> dict[str, str]:
    header: dict[str, str] = {}
    for line in text.splitlines()[:40]:
        m = _HEAD_RE.match(line)
        if m:
            header[m.group(1)] = m.group(2).strip()
    return header


def job_is_valid(header: dict[str, str]) -> bool:
    name = header.get("name", "").strip()
    cron = header.get("cron", "").strip()
    if not name or not cron:
        return False
    return next_after(cron, _dt.datetime.now()) is not None


def rewrite_header(file: Path, changes: dict[str, str]) -> None:
    """就地改写已存在的 `# key:` 头；未存在的键忽略（部署时保证头完整）。"""
    if not changes:
        return
    text = file.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    keys = set(changes)
    out: list[str] = []
    for line in lines[:40]:
        m = _HEAD_RE.match(line)
        if m and m.group(1) in keys:
            out.append(f"# {m.group(1)}: {changes[m.group(1)]}")
            keys.discard(m.group(1))
        else:
            out.append(line)
    out.extend(lines[40:])
    file.write_text("\n".join(out), encoding="utf-8")


class JobRegistry:
    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir) / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self.reload()

    def reload(self) -> int:
        """全量重扫。仅含合法 name+cron 声明的文件注册为任务。"""
        jobs: dict[str, Job] = {}
        for p in self.dir.glob("*.py"):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            h = parse_header(text)
            if not job_is_valid(h):
                continue
            name = h["name"].strip()
            jobs[name] = Job(
                name=name,
                cron=h["cron"].strip(),
                connection=h.get("connection", "").strip(),
                enabled=h.get("enabled", "true").strip().lower() not in ("0", "false", "no"),
                system=h.get("system", "").strip().lower() in ("true", "1", "yes"),
                file=p.name,
            )
        self._jobs = jobs
        return len(jobs)

    def list(self) -> list[Job]:
        return list(self._jobs.values())

    def get(self, name: str) -> Job | None:
        return self._jobs.get(name)

    def has(self, name: str) -> bool:
        return name in self._jobs

    def file(self, name: str) -> Path | None:
        j = self._jobs.get(name)
        return self.dir / j.file if j else None

    def path(self, name: str) -> Path | None:
        return self.file(name)