"""旧任务模型迁移测试：tasks.db → jobs/*.py（一次性幂等）。"""
from __future__ import annotations

import sqlite3

from app.tasks.jobs import JobRegistry
from app.tasks.migrate import MARKER, migrate_legacy_tasks


def _seed_legacy(data_dir) -> None:
    con = sqlite3.connect(str(data_dir / "tasks.db"))
    con.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, cron TEXT NOT NULL, sql TEXT,
            natural_query TEXT, connection_id TEXT NOT NULL, skill TEXT DEFAULT 'query',
            enabled INTEGER DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_run_at TEXT
        );
        """
    )
    con.execute("INSERT INTO tasks (id, name, cron, sql, connection_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                ("t1", "旧日报", "0 9 * * *", "SELECT COUNT(*) FROM orders", "c1", "a", "b"))
    # natural_query-only
    con.execute("INSERT INTO tasks (id, name, cron, natural_query, connection_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                ("t2", "旧周报", "0 10 * * 1", "统计本周", "c1", "a", "b"))
    con.commit()
    con.close()


def test_migrate_converts_and_is_idempotent(tmp_path):
    _seed_legacy(tmp_path)
    n = migrate_legacy_tasks(tmp_path)
    assert n == 2
    assert (tmp_path / "jobs" / MARKER).exists()
    reg = JobRegistry(tmp_path)
    names = {j.name for j in reg.list()}
    assert "旧日报" in names  # sql 任务 → 可运行脚本
    # 旧自然语言任务 → 禁用桩
    stub = reg.get("旧周报")
    assert stub is not None and stub.enabled is False
    # 幂等：二次迁移不新增
    assert migrate_legacy_tasks(tmp_path) == 0
    assert len(reg.list()) == len(JobRegistry(tmp_path).list())


def test_migrate_no_legacy_db(tmp_path):
    assert migrate_legacy_tasks(tmp_path) == 0
    assert (tmp_path / "jobs" / MARKER).exists()