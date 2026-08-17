"""审计日志：每条执行语句 JSONL 追加记录。未来企业版可同步到团队审计服务器。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class AuditLogger:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "audit.log"
        self._lock = threading.Lock()

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
    ) -> None:
        first_line = " ".join((sql or "").strip().splitlines()[:1])[:200]
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "connection": connection,
            "origin": origin,
            "tier": tier,
            "verdict": verdict,
            "status": status,
            "sql": first_line,
        }
        if elapsed_ms is not None:
            entry["elapsed_ms"] = elapsed_ms
        if report_id is not None:
            entry["report_id"] = report_id
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def list(
        self,
        connection: str | None = None,
        verdict: str | None = None,
        limit: int = 100,
        from_ts: str | None = None,
        to_ts: str | None = None,
        report_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with self._lock:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if connection and e.get("connection") != connection:
                    continue
                if verdict and e.get("verdict") != verdict:
                    continue
                if from_ts and e.get("ts", "") < from_ts:
                    continue
                if to_ts and e.get("ts", "") > to_ts:
                    continue
                if report_id and e.get("report_id") != report_id:
                    continue
                entries.append(e)
        return entries[-limit:]
