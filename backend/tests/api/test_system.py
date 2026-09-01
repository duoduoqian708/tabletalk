"""系统保留端点：审计/聊天/成本日志按天清理（可追溯）。"""
from __future__ import annotations

import datetime as dt
import sqlite3


def _iso(offset_days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=offset_days)).isoformat(timespec="milliseconds")


async def test_retain_audit_removes_old_keeps_recent(client, app_state):
    db = app_state.env.data_dir / "audit.db"
    con = sqlite3.connect(db)
    con.executemany(
        "INSERT INTO audit_log (schema_version, ts, connection, origin, tier, verdict, status, sql) VALUES (1,?,?,?,?,?,?,?)",
        [(_iso(200), "c", "manual", "read", "allow", "s", "old1"),
         (_iso(200), "c", "manual", "read", "allow", "s", "old2"),
         (_iso(1), "c", "manual", "read", "allow", "s", "recent")],
    )
    con.commit()
    con.close()

    r = await client.post("/api/v1/system/retain", json={"audit": 90})
    assert r.status_code == 200
    assert r.json()["deleted"]["audit"] == 2

    con = sqlite3.connect(db)
    old_left = con.execute("SELECT COUNT(*) FROM audit_log WHERE ts < ?", (_iso(90),)).fetchone()[0]
    recent = con.execute("SELECT COUNT(*) FROM audit_log WHERE sql='recent'").fetchone()[0]
    con.close()
    assert old_left == 0
    assert recent == 1


async def test_retain_chat_cascades(client, app_state):
    db = app_state.env.data_dir / "chat.db"
    con = sqlite3.connect(db)
    con.execute("INSERT INTO conversations (id, connection_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                ("old_conv", "c", "老会话", _iso(200), _iso(200)))
    con.execute("INSERT INTO messages (conversation_id, role, kind, content, created_at) VALUES (?,?,?,?,?)",
                ("old_conv", "user", "text", "hi", _iso(200)))
    con.execute("INSERT INTO artifacts (result_id, session_id, data, created_at) VALUES (?,?,?,?)",
                ("art1", "old_conv", "{}", _iso(200)))
    con.execute("INSERT INTO conversations (id, connection_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                ("new_conv", "c", "新会话", _iso(1), _iso(1)))
    con.commit()
    con.close()

    r = await client.post("/api/v1/system/retain", json={"chat": 90})
    assert r.status_code == 200
    assert r.json()["deleted"]["chat"] == 1

    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM conversations WHERE id='old_conv'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM messages WHERE conversation_id='old_conv'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM artifacts WHERE session_id='old_conv'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM conversations WHERE id='new_conv'").fetchone()[0] == 1
    con.close()


async def test_retain_cost(client, app_state):
    db = app_state.env.data_dir / "cost.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE IF NOT EXISTS cost_log (ts TEXT, connection TEXT, skill TEXT, model TEXT, provider TEXT, "
        "input_tokens INT, output_tokens INT, total_tokens INT, elapsed_ms INT, estimated_cost_usd REAL)"
    )
    con.executemany(
        "INSERT INTO cost_log (ts, connection, skill, model, provider, input_tokens, output_tokens, total_tokens, elapsed_ms, estimated_cost_usd) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(_iso(200), "c", "q", "m", "p", 1, 1, 2, 1, 0.0),
         (_iso(1), "c", "q", "m", "p", 1, 1, 2, 1, 0.0)],
    )
    con.commit()
    con.close()

    r = await client.post("/api/v1/system/retain", json={"cost": 90})
    assert r.status_code == 200
    assert r.json()["deleted"]["cost"] == 1


async def test_retain_leaves_audit_trace(client, app_state):
    await client.post("/api/v1/system/retain", json={"audit": 90})
    con = sqlite3.connect(app_state.env.data_dir / "audit.db")
    n = con.execute("SELECT COUNT(*) FROM audit_log WHERE source='scheduled' AND sql LIKE '%retain%'").fetchone()[0]
    con.close()
    assert n >= 1