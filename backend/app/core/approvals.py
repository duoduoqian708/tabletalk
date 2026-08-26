"""DML 审批流 — SQLite 持久化（tabletalk.db: approvals）。"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.core.system_db import get_conn, init_system_db


@dataclass
class Approval:
    id: str
    connection_id: str
    sql: str
    requested_by: str
    requested_at: str
    status: str = "pending"
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    note: str | None = None
    preview_rows: int | None = None
    executed_audit_id: int | None = None
    rollback_ref: str | None = None

class ApprovalStore:
    def __init__(self, data_dir: Path):
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / "approvals.json"
        self._items: dict[str, Approval] = {}
        init_system_db(self._data_dir)
        self._load()
        self._migrate_if_needed()

    def _load(self):
        try:
            con = get_conn(self._data_dir)
            cur = con.execute("SELECT data FROM approvals")
            for (data_json,) in cur.fetchall():
                try:
                    item = json.loads(data_json)
                    a = Approval(**item)
                    self._items[a.id] = a
                except Exception:
                    continue
            con.close()
        except Exception:
            pass

    def _migrate_if_needed(self):
        if self._items:
            return
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for item in data:
                try:
                    a = Approval(**item)
                    self._items[a.id] = a
                except Exception:
                    continue
            if self._items:
                self._save()
                try:
                    bak = self._path.with_suffix(".json.bak")
                    if not bak.exists():
                        self._path.rename(bak)
                except OSError:
                    pass
        except Exception:
            pass

    def _save(self):
        try:
            con = get_conn(self._data_dir)
            con.execute("DELETE FROM approvals")
            for a in self._items.values():
                con.execute(
                    "INSERT INTO approvals (id, data, status, created_at) VALUES (?,?,?,?)",
                    (a.id, json.dumps(asdict(a), ensure_ascii=False), a.status, a.requested_at),
                )
            con.commit()
            con.close()
        except Exception:
            pass
        if self._path.exists():
            try:
                bak = self._path.with_suffix(".json.bak")
                if not bak.exists():
                    self._path.rename(bak)
            except OSError:
                pass

    def create(self, connection_id: str, sql: str, requested_by: str) -> Approval:
        a = Approval(id=f"ap_{uuid.uuid4().hex[:8]}", connection_id=connection_id, sql=sql, requested_by=requested_by, requested_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        self._items[a.id] = a
        self._save()
        return a

    def get(self, aid: str) -> Approval | None:
        return self._items.get(aid)

    def list(self, status: str | None = None) -> list[Approval]:
        if status:
            return [a for a in self._items.values() if a.status == status]
        return list(self._items.values())

    def approve(self, aid: str, reviewer: str, note: str | None = None) -> Approval | None:
        a = self._items.get(aid)
        if not a or a.status != "pending":
            return None
        a.status = "approved"
        a.reviewed_by = reviewer
        a.reviewed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        a.note = note
        self._save()
        return a

    def reject(self, aid: str, reviewer: str, note: str | None = None) -> Approval | None:
        a = self._items.get(aid)
        if not a or a.status != "pending":
            return None
        a.status = "rejected"
        a.reviewed_by = reviewer
        a.reviewed_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        a.note = note
        self._save()
        return a

    def attach_execution(self, aid: str, *, executed_audit_id: int, rollback_ref: str | None = None) -> Approval | None:
        a = self._items.get(aid)
        if not a:
            return None
        a.executed_audit_id = executed_audit_id
        if rollback_ref:
            a.rollback_ref = rollback_ref
        self._save()
        return a

    def set_preview(self, aid: str, preview_rows: int | None) -> None:
        a = self._items.get(aid)
        if a:
            a.preview_rows = preview_rows
            self._save()
