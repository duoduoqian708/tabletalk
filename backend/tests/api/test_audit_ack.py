"""audit_ack 未读体系 + log() 返回行 id。"""
from __future__ import annotations

from app.audit.logger import AuditLogger


def _log(lg: AuditLogger, verdict: str, conn: str = "demo"):
    return lg.log(connection=conn, origin="ai", tier="dml", verdict=verdict,
                  status="x", sql="UPDATE t SET a=1 WHERE id=1")


def test_log_returns_rowid(tmp_path):
    lg = AuditLogger(tmp_path)
    rid = _log(lg, "review")
    assert isinstance(rid, int) and rid > 0


def test_unread_count_and_ack(tmp_path):
    lg = AuditLogger(tmp_path)
    r1 = _log(lg, "block")
    _log(lg, "review")
    _log(lg, "allow")            # 放行不算异常
    assert lg.unread_exception_count() == 2
    lg.ack(r1)
    assert lg.unread_exception_count() == 1
    assert lg.ack_state(r1) == "ack"


def test_list_includes_ack_field(tmp_path):
    lg = AuditLogger(tmp_path)
    r1 = _log(lg, "block")
    entries = lg.list()
    assert entries[0]["ack"] == "unread"
    lg.ack(r1)
    assert lg.list()[0]["ack"] == "ack"


def test_unread_filter_by_connection(tmp_path):
    lg = AuditLogger(tmp_path)
    _log(lg, "block", conn="a")
    _log(lg, "block", conn="b")
    assert lg.unread_exception_count(connection="a") == 1
