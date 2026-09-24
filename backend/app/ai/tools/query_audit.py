"""query_audit 工具：查询审计日志，支持时间/verdict/连接/来源过滤 + 最近N条快捷模式。

合并了旧 get_last_operations 的功能（最近N条 = 按时间倒序取前N条，限当前连接）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.ai.tools.registry import ToolOutcome, register_tool

if TYPE_CHECKING:
    from app.state import AppState

DEFAULT_LIMIT = 20


async def _query_audit(state: "AppState", args: dict[str, Any], conn_id: str, include_data: bool = False) -> ToolOutcome:
    from app.safety.redact import redact_text
    from app.config import get_env

    from_ts = (args or {}).get("from_ts")
    to_ts = (args or {}).get("to_ts")
    verdict = (args or {}).get("verdict")
    origin = (args or {}).get("origin")
    connection = (args or {}).get("connection")
    recent = (args or {}).get("recent")  # 快捷模式：最近N条
    limit = int((args or {}).get("limit") or DEFAULT_LIMIT)
    offset = int((args or {}).get("offset") or 0)

    # recent 模式：取当前连接最近 N 条（时间倒序）
    if recent:
        try:
            recent_n = min(int(recent), 100)
        except (ValueError, TypeError):
            recent_n = 10
        # 用当前连接
        if not connection:
            try:
                cfg = state.connections.get(conn_id)
                connection = cfg.name if cfg else conn_id
            except Exception:
                connection = conn_id
        all_entries = state.audit.list(connection=connection)
        # 时间倒序
        all_entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
        entries = all_entries[:recent_n]
    else:
        # 标准过滤模式
        try:
            cfg = state.connections.get(conn_id)
            default_conn = cfg.name if cfg else None
        except Exception:
            default_conn = None
        all_entries = state.audit.list(
            connection=connection or default_conn,
            origin=origin,
            verdict=verdict,
            from_ts=from_ts,
            to_ts=to_ts,
        )
        # 时间倒序 + 分页
        all_entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
        entries = all_entries[offset: offset + limit]

    # 脱敏处理（standard 模式：SQL 字段过 redact_text；open 模式不脱敏）
    try:
        data_dir = get_env().data_dir
        from app.safety.redact import get_salt
        salt = get_salt(data_dir)
        try:
            sensitive = state.connections.get(conn_id).sensitive
        except Exception:
            sensitive = []
        # open 模式不脱敏
        privacy_mode = "standard"
        try:
            privacy_mode = state.runtime.get().privacy_mode
        except Exception:
            pass
        if privacy_mode != "open":
            for entry in entries:
                sql = entry.get("sql", "")
                if sql and salt:
                    redacted, _ = redact_text(sql, salt, sensitive)
                    entry["sql"] = redacted
    except Exception:
        pass

    total = len(all_entries) if not recent else len(entries)
    return ToolOutcome(
        result={
            "ok": True,
            "entries": entries,
            "total": total,
            "limit": limit,
            "offset": offset,
        },
        think=f"查询到 {len(entries)} 条审计记录。",
    )


def register() -> None:
    register_tool(
        "query_audit",
        "查询审计日志：按时间范围/verdict/连接/来源过滤，或用 recent 参数取最近N条。",
        {
            "from_ts": {"type": "string", "description": "起始时间（YYYY-MM-DDTHH:MM:SS）"},
            "to_ts": {"type": "string", "description": "结束时间（YYYY-MM-DDTHH:MM:SS）"},
            "verdict": {"type": "string", "enum": ["allow", "review", "block", "egress"], "description": "过滤判定结果"},
            "origin": {"type": "string", "description": "过滤来源（ai/manual/system_tool）"},
            "connection": {"type": "string", "description": "过滤连接名（不传=当前连接）"},
            "recent": {"type": "integer", "description": "快捷模式：取最近N条（限当前连接，最大100）"},
            "limit": {"type": "integer", "description": "分页大小（默认20）"},
            "offset": {"type": "integer", "description": "分页偏移（默认0）"},
        },
        [],
        _query_audit,
        trust="readonly",
        audit_source="system_tool",
    )
