"""审计日志：SQLite 持久化（A1 可解释 + 海量可查），兼容旧 JSONL 迁移。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


class AuditLogger:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "audit.log"  # 旧文件，迁移后归档为 audit.log.bak
        self.db_path = data_dir / "audit.db"
        self._lock = threading.Lock()
        self._init_db()
        self._migrate_jsonl_if_needed()

    def _init_db(self) -> None:
        with self._lock:
            con = sqlite3.connect(self.db_path)
            try:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        schema_version INTEGER NOT NULL,
                        ts TEXT NOT NULL,
                        connection TEXT NOT NULL,
                        origin TEXT NOT NULL,
                        tier TEXT NOT NULL,
                        verdict TEXT NOT NULL,
                        status TEXT NOT NULL,
                        sql TEXT NOT NULL,
                        elapsed_ms REAL,
                        report_id TEXT,
                        source TEXT,
                        reasons TEXT,
                        tables_json TEXT,
                        manifest TEXT,
                        approval_id TEXT,
                        rollback_ref TEXT,
                        estimated_rows INTEGER,
                        cost_degraded TEXT,
                        extra_json TEXT
                    )
                    """
                )
                con.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)")
                con.execute("CREATE INDEX IF NOT EXISTS idx_audit_connection ON audit_log(connection)")
                con.execute("CREATE INDEX IF NOT EXISTS idx_audit_verdict ON audit_log(verdict)")
                con.execute("CREATE INDEX IF NOT EXISTS idx_audit_origin ON audit_log(origin)")
                con.execute("CREATE INDEX IF NOT EXISTS idx_audit_report_id ON audit_log(report_id)")
                con.execute("CREATE INDEX IF NOT EXISTS idx_audit_source ON audit_log(source)")
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS audit_ack (
                        audit_id INTEGER PRIMARY KEY REFERENCES audit_log(id),
                        state TEXT NOT NULL,
                        acked_ts TEXT
                    )
                    """
                )
                con.commit()
            finally:
                con.close()

    def _migrate_jsonl_if_needed(self) -> None:
        # 仅当 db 为空且旧文件存在时迁移
        if not self.path.exists():
            return
        with self._lock:
            con = sqlite3.connect(self.db_path)
            try:
                cur = con.execute("SELECT COUNT(*) FROM audit_log")
                cnt = cur.fetchone()[0]
                if cnt > 0:
                    return
                # 读旧文件
                try:
                    lines = self.path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    return
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    # 映射旧 entry 到新表
                    con.execute(
                        """
                        INSERT INTO audit_log (
                            schema_version, ts, connection, origin, tier, verdict, status, sql,
                            elapsed_ms, report_id, source, reasons, tables_json, manifest,
                            approval_id, rollback_ref, estimated_rows, cost_degraded, extra_json
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            e.get("schema_version", SCHEMA_VERSION),
                            e.get("ts", time.strftime("%Y-%m-%dT%H:%M:%S")),
                            e.get("connection", ""),
                            e.get("origin", ""),
                            e.get("tier", ""),
                            e.get("verdict", ""),
                            e.get("status", ""),
                            e.get("sql", "")[:200],
                            e.get("elapsed_ms"),
                            e.get("report_id"),
                            e.get("source"),
                            json.dumps(e.get("reasons"), ensure_ascii=False) if e.get("reasons") is not None else None,
                            json.dumps(e.get("tables"), ensure_ascii=False) if e.get("tables") is not None else None,
                            json.dumps(e.get("manifest"), ensure_ascii=False) if e.get("manifest") is not None else None,
                            e.get("approval_id"),
                            e.get("rollback_ref"),
                            e.get("estimated_rows"),
                            e.get("cost_degraded"),
                            json.dumps({k: v for k, v in e.items() if k not in {
                                "schema_version","ts","connection","origin","tier","verdict","status","sql",
                                "elapsed_ms","report_id","source","reasons","tables","manifest",
                                "approval_id","rollback_ref","estimated_rows","cost_degraded"
                            }}, ensure_ascii=False) if any(k not in {
                                "schema_version","ts","connection","origin","tier","verdict","status","sql",
                                "elapsed_ms","report_id","source","reasons","tables","manifest",
                                "approval_id","rollback_ref","estimated_rows","cost_degraded"
                            } for k in e) else None,
                        ),
                    )
                con.commit()
                # 归档旧文件
                try:
                    bak = self.path.with_suffix(".log.bak")
                    if not bak.exists():
                        self.path.rename(bak)
                except OSError:
                    pass
            finally:
                con.close()

    def log(
        self,
        *,
        connection: str,
        origin: str,
        tier: str,
        verdict: str,
        status: str,
        sql: str,
        elapsed_ms: float | None = None,
        report_id: str | None = None,
        source: str | None = None,
        reasons: list[dict[str, Any]] | None = None,
        tables: list[str] | None = None,
        manifest: dict[str, Any] | None = None,
        approval_id: str | None = None,
        rollback_ref: str | None = None,
        **extra: Any,
    ) -> int:
        first_line = " ".join((sql or "").strip().splitlines()[:1])[:200]
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        # reasons 兜底
        if reasons is not None:
            if verdict in ("block", "review") and len(reasons) == 0:
                reasons = [{"rule_id": "unknown", "message": status or verdict, "message_en": status or verdict, "objects": []}]
        elif verdict in ("block", "review"):
            reasons = [{"rule_id": "unknown", "message": status or verdict, "message_en": status or verdict, "objects": []}]
        reasons_json = json.dumps(reasons, ensure_ascii=False) if reasons is not None else None
        tables_json = json.dumps(tables, ensure_ascii=False) if tables is not None else None
        manifest_json = json.dumps(manifest, ensure_ascii=False) if manifest is not None else None
        # extra 中已知列单独提，非已知入 extra_json
        estimated_rows = extra.pop("estimated_rows", None)
        cost_degraded = extra.pop("cost_degraded", None)
        # 剩余 extra
        extra_json = json.dumps(extra, ensure_ascii=False) if extra else None
        with self._lock:
            con = sqlite3.connect(self.db_path)
            try:
                cur = con.execute(
                    """
                    INSERT INTO audit_log (
                        schema_version, ts, connection, origin, tier, verdict, status, sql,
                        elapsed_ms, report_id, source, reasons, tables_json, manifest,
                        approval_id, rollback_ref, estimated_rows, cost_degraded, extra_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        SCHEMA_VERSION,
                        ts,
                        connection,
                        origin,
                        tier,
                        verdict,
                        status,
                        first_line,
                        elapsed_ms,
                        report_id,
                        source,
                        reasons_json,
                        tables_json,
                        manifest_json,
                        approval_id,
                        rollback_ref,
                        int(estimated_rows) if estimated_rows is not None else None,
                        str(cost_degraded) if cost_degraded is not None else None,
                        extra_json,
                    ),
                )
                con.commit()
                return int(cur.lastrowid)
            finally:
                con.close()

    def list(
        self,
        connection: str | None = None,
        origin: str | None = None,
        tier: str | None = None,
        verdict: str | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
        report_id: str | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """返回全量过滤结果（不窗口化）；分页由调用方按时间倒序切片。"""
        with self._lock:
            con = sqlite3.connect(self.db_path)
            con.row_factory = sqlite3.Row
            try:
                where = []
                params: list[Any] = []
                if connection:
                    where.append("a.connection = ?")
                    params.append(connection)
                if origin:
                    where.append("a.origin = ?")
                    params.append(origin)
                if tier:
                    where.append("a.tier = ?")
                    params.append(tier)
                if verdict:
                    where.append("a.verdict = ?")
                    params.append(verdict)
                if from_ts:
                    where.append("a.ts >= ?")
                    params.append(from_ts)
                if to_ts:
                    where.append("a.ts <= ?")
                    params.append(to_ts)
                if report_id:
                    where.append("a.report_id = ?")
                    params.append(report_id)
                if source:
                    where.append("a.source = ?")
                    params.append(source)
                sql = "SELECT a.*, k.state AS ack_state FROM audit_log a LEFT JOIN audit_ack k ON k.audit_id = a.id"
                if where:
                    sql += " WHERE " + " AND ".join(where)
                sql += " ORDER BY id ASC"
                cur = con.execute(sql, params)
                rows = cur.fetchall()
                out: list[dict[str, Any]] = []
                for r in rows:
                    e: dict[str, Any] = {
                        "schema_version": r["schema_version"],
                        "ts": r["ts"],
                        "connection": r["connection"],
                        "origin": r["origin"],
                        "tier": r["tier"],
                        "verdict": r["verdict"],
                        "status": r["status"],
                        "sql": r["sql"],
                    }
                    if r["elapsed_ms"] is not None:
                        e["elapsed_ms"] = r["elapsed_ms"]
                    if r["report_id"] is not None:
                        e["report_id"] = r["report_id"]
                    if r["source"] is not None:
                        e["source"] = r["source"]
                    if r["reasons"] is not None:
                        try:
                            e["reasons"] = json.loads(r["reasons"])
                        except Exception:
                            e["reasons"] = []
                    elif r["verdict"] in ("block", "review"):
                        e["reasons"] = [{"rule_id": "unknown", "message": r["status"] or r["verdict"], "message_en": r["status"] or r["verdict"], "objects": []}]
                    if r["tables_json"] is not None:
                        try:
                            e["tables"] = json.loads(r["tables_json"])
                        except Exception:
                            pass
                    if r["manifest"] is not None:
                        try:
                            e["manifest"] = json.loads(r["manifest"])
                        except Exception:
                            pass
                    if r["approval_id"] is not None:
                        e["approval_id"] = r["approval_id"]
                    if r["rollback_ref"] is not None:
                        e["rollback_ref"] = r["rollback_ref"]
                    if r["estimated_rows"] is not None:
                        e["estimated_rows"] = r["estimated_rows"]
                    if r["cost_degraded"] is not None:
                        e["cost_degraded"] = r["cost_degraded"]
                    if r["extra_json"] is not None:
                        try:
                            extra = json.loads(r["extra_json"])
                            if isinstance(extra, dict):
                                e.update(extra)
                        except Exception:
                            pass
                    e["ack"] = r["ack_state"] or "unread"
                    out.append(e)
                return out
            finally:
                con.close()

    def ack(self, audit_id: int) -> None:
        """标记某条审计为已处理（幂等）。"""
        with self._lock:
            con = sqlite3.connect(self.db_path)
            con.execute("PRAGMA foreign_keys = ON")
            try:
                con.execute(
                    "INSERT OR REPLACE INTO audit_ack (audit_id, state, acked_ts) VALUES (?,?,?)",
                    (int(audit_id), "ack", time.strftime("%Y-%m-%dT%H:%M:%S")),
                )
                con.commit()
            finally:
                con.close()

    def ack_state(self, audit_id: int) -> str:
        """查询某条审计的处理状态，无记录视为未读。"""
        with self._lock:
            con = sqlite3.connect(self.db_path)
            try:
                cur = con.execute("SELECT state FROM audit_ack WHERE audit_id=?", (int(audit_id),))
                row = cur.fetchone()
                return row[0] if row else "unread"
            finally:
                con.close()

    def unread_exception_count(self, connection: str | None = None) -> int:
        """未读异常数 = verdict∈{block,review} 且无 ack 记录。"""
        sql = (
            "SELECT COUNT(*) FROM audit_log a "
            "LEFT JOIN audit_ack k ON k.audit_id = a.id "
            "WHERE a.verdict IN ('block','review') AND k.audit_id IS NULL"
        )
        params: list[Any] = []
        if connection:
            sql += " AND a.connection = ?"
            params.append(connection)
        with self._lock:
            con = sqlite3.connect(self.db_path)
            try:
                return int(con.execute(sql, params).fetchone()[0])
            finally:
                con.close()
