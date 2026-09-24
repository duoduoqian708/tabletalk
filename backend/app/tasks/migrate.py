"""旧任务模型迁移（一次性，幂等）：tasks.db `{sql|natural_query}` → jobs/*.py 脚本。

旧模型是"表单式 SQL/自然语言任务"；新模型全部脚本化。首启时把旧任务转成等价脚本，
natural_query-only 的任务转为禁用桩（提示用 AI 对话重建）。成功迁移后写标记文件。
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

MARKER = ".migrated-v1"

_SQL_TEMPLATE = """# name: {name}
# cron: {cron}
# connection: {conn}
# enabled: {enabled}
from lib import query, summary

if __name__ == "__main__":
    rows = query("__SQL__")
    summary(f"查询返回 {{len(rows)}} 行")
"""

_STUB_TEMPLATE = """# name: {name}
# cron: {cron}
# connection: {conn}
# enabled: 0
# 旧版自然语言任务：无法直接转脚本，请到「定时任务」页用 AI 对话重建。
from lib import summary

if __name__ == "__main__":
    summary("旧版自然语言任务已下线，请用 AI 对话重建")
"""


def _slug(name: str) -> str:
    s = re.sub(r"[^\w一-鿿]+", "_", (name or "").strip()).strip("_")
    return (s or "task")[:40] + ".py"


def migrate_legacy_tasks(data_dir: Path) -> int:
    jobs = Path(data_dir) / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    if (jobs / MARKER).exists():
        return 0
    db = Path(data_dir) / "tasks.db"
    n = 0
    if db.exists():
        try:
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT * FROM tasks").fetchall()
            con.close()
        except Exception:  # noqa: BLE001
            rows = []
        for r in rows:
            try:
                name = r["name"] or "task"
                cron = r["cron"] or "0 9 * * *"
                conn = r["connection_id"] or ""
                enabled = 1 if r["enabled"] else 0
                sql = (r["sql"] or "").strip()
                f = jobs / _slug(name)
                if f.exists():
                    continue
                if sql:
                    body = _SQL_TEMPLATE.format(name=name, cron=cron, conn=conn, enabled=enabled).replace(
                        "__SQL__", sql.replace('"', "'")
                    )
                else:
                    body = _STUB_TEMPLATE.format(name=name, cron=cron, conn=conn)
                f.write_text(body, encoding="utf-8")
                n += 1
            except Exception:  # noqa: BLE001 - 单项失败不阻塞整体迁移
                continue
    (jobs / MARKER).write_text("1", encoding="utf-8")
    return n