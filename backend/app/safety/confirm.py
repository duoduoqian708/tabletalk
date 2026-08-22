"""WS4 T4.1 DML 确认 token 状态机：一次性、10 分钟过期、绑 SQL 哈希（防 TOCTOU）。

设计（08 §6 / D11）：
- `run_dml` 返回 REVIEW 时生成 token=uuid4().hex，把 pending_dml 落会话（chat_store.pending_dmls）。
- 确认执行（POST /query 带 confirm_token）时 `validate_and_consume`：未过期 + 未消费 + SQL 哈希一致 -> 置 consumed 放行。
- 任一不满足 -> 拒绝并要求重走 preview。取消/放过期由调用方 clear_pending_dml。
- 确认只是"人看过预览"的凭证，不是通行证——T4.3 执行前仍要重新过闸门。
"""
from __future__ import annotations

import hashlib
import time
import uuid

TOKEN_TTL = 10 * 60  # 10 分钟过期


def _now() -> int:
    return int(time.time())


def _hash_sql(sql: str) -> str:
    return hashlib.sha256((sql or "").encode("utf-8")).hexdigest()


def create_pending(store, session_id: str, sql: str, preview: int | None, rollback: str | None,
                   ttl: int = TOKEN_TTL) -> dict:
    """生成并落一份待确认 DML，返回 payload（含 token / sql / sql_hash / preview / rollback / expires_at / consumed=False）。"""
    now = _now()
    payload = {
        "token": uuid.uuid4().hex,
        "sql": sql,
        "sql_hash": _hash_sql(sql),
        "preview": preview,
        "rollback": rollback,
        "created_at": now,
        "expires_at": now + ttl,
        "consumed": False,
    }
    store.set_pending_dml(session_id, payload)
    return payload


def get_pending(store, session_id: str) -> dict | None:
    return store.get_pending_dml(session_id)


def validate_and_consume(store, session_id: str, token: str, sql: str) -> tuple[bool, str]:
    """校验并消费确认 token：未过期 + 未消费 + SQL 哈希一致 -> 置 consumed 并放行。
    返回 (ok, reason)；任一不满足 -> (False, 明确原因) 要求重走 preview。不抛出。"""
    pending = store.get_pending_dml(session_id)
    if not pending:
        return False, "没有待确认的写操作（可能已过期或已被处理）"
    if pending.get("token") != token:
        return False, "确认 token 不匹配，请重新预览"
    if pending.get("consumed"):
        return False, "该确认已被使用过，请重新预览"
    if int(time.time()) > pending.get("expires_at", 0):
        return False, "确认已过期，请重新预览后再确认"
    if _hash_sql(sql) != pending.get("sql_hash"):
        return False, "待执行 SQL 与预览时不一致（可能被篡改），请重新预览后再确认"
    pending["consumed"] = True
    store.set_pending_dml(session_id, pending)  # 持久化 consumed，防二次消费
    return True, "ok"


def clear_pending(store, session_id: str) -> bool:
    """取消/过期后清除；返回之前是否存在（供写回"用户拒绝"历史）。"""
    was = store.get_pending_dml(session_id) is not None
    store.clear_pending_dml(session_id)
    return was