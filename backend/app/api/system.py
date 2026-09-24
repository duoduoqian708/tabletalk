"""平台系统端点：日志保留清理。

脚本经 SDK retain_logs() 调用；删除权与审计归平台所有——脚本只声明"保留 N 天"。
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import get_env
from app.state import get_state

router = APIRouter(prefix="/api/v1/system", tags=["system"])


class RetainRequest(BaseModel):
    audit: int | None = None    # 审计日志保留天数
    chat: int | None = None     # 聊天会话保留天数
    cost: int | None = None     # 成本日志保留天数
    results: int | None = None  # 报告结果保留最新 N 条（按总数，非天数）


def _cutoff_iso(days: int) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=max(1, days))).isoformat(timespec="milliseconds")


def _delete_audit(db: Path, cutoff: str) -> int:
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        cur = con.execute("DELETE FROM audit_log WHERE ts < ? AND ts != ''", (cutoff,))
        n = int(cur.rowcount)
        con.execute("DELETE FROM audit_ack WHERE audit_id NOT IN (SELECT id FROM audit_log)")
        con.commit()
        return n
    finally:
        con.close()


def _delete_chat(db: Path, cutoff: str) -> int:
    con = sqlite3.connect(db)
    try:
        cur = con.execute("SELECT id FROM conversations WHERE created_at < ? AND created_at != ''", (cutoff,))
        ids = [r[0] for r in cur.fetchall()]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        con.execute(f"DELETE FROM messages WHERE conversation_id IN ({marks})", ids)
        con.execute(f"DELETE FROM artifacts WHERE session_id IN ({marks})", ids)
        con.execute(f"DELETE FROM pending_dmls WHERE session_id IN ({marks})", ids)
        con.execute(f"DELETE FROM conversations WHERE id IN ({marks})", ids)
        con.commit()
        return len(ids)
    finally:
        con.close()


def _delete_cost(db: Path, cutoff: str) -> int:
    con = sqlite3.connect(db)
    try:
        cur = con.execute("DELETE FROM cost_log WHERE ts < ? AND ts != ''", (cutoff,))
        n = int(cur.rowcount)
        con.commit()
        return n
    finally:
        con.close()


@router.post("/retain")
async def retain(req: RetainRequest) -> dict[str, Any]:
    state = get_state()
    data_dir = Path(get_env().data_dir)
    deleted: dict[str, int] = {}
    if req.audit:
        deleted["audit"] = _delete_audit(data_dir / "audit.db", _cutoff_iso(req.audit))
    if req.chat:
        deleted["chat"] = _delete_chat(data_dir / "chat.db", _cutoff_iso(req.chat))
    if req.cost:
        deleted["cost"] = _delete_cost(data_dir / "cost.db", _cutoff_iso(req.cost))
    if req.results:
        deleted["results"] = state.results.cleanup(keep_n=req.results)
    # 操作本身留痕（可追溯清理行为）
    try:
        import json as _json

        state.audit.log(
            connection="system",
            origin="scheduled",
            tier="job",
            verdict="executed",
            status="log_retention",
            sql=f"[retain] {_json.dumps(deleted, ensure_ascii=False, sort_keys=True)}",
            source="scheduled",
        )
    except Exception:  # noqa: BLE001
        pass
    return {"deleted": deleted}