"""DML 审批流 — E2 最小闭环（DBA 一键批/驳，审计链）。"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

@dataclass
class Approval:
    id: str
    connection_id: str
    sql: str
    requested_by: str
    requested_at: str
    status: str = "pending"  # pending | approved | rejected
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    note: str | None = None

class ApprovalStore:
    def __init__(self, data_dir: Path):
        self._path = Path(data_dir) / "approvals.json"
        self._items: dict[str, Approval] = {}
        self._load()

    def _load(self):
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for item in data:
                a = Approval(**item)
                self._items[a.id] = a
        except Exception:
            pass

    def _save(self):
        try:
            self._path.write_text(json.dumps([asdict(a) for a in self._items.values()], ensure_ascii=False, indent=2), encoding="utf-8")
            self._path.chmod(0o600)
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
