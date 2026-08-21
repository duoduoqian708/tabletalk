"""审计日志：每条执行语句 JSONL 追加记录（A1 可解释：reasons 结构化）。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


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
        source: str | None = None,
        reasons: list[dict[str, Any]] | None = None,
        tables: list[str] | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> None:
        first_line = " ".join((sql or "").strip().splitlines()[:1])[:200]
        entry: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
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
        if source is not None:
            entry["source"] = source
        if reasons is not None:
            if verdict in ("block", "review") and len(reasons) == 0:
                entry["reasons"] = [{"rule_id": "unknown", "message": status or verdict, "message_en": status or verdict, "objects": []}]
            else:
                entry["reasons"] = reasons
        elif verdict in ("block", "review"):
            entry["reasons"] = [{"rule_id": "unknown", "message": status or verdict, "message_en": status or verdict, "objects": []}]
        if tables is not None:
            entry["tables"] = tables
        if manifest is not None:
            entry["manifest"] = manifest
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
        origin: str | None = None,
        tier: str | None = None,
        verdict: str | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
        report_id: str | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """返回全量过滤结果（不窗口化）；分页由调用方按时间倒序切片。"""
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
                if origin and e.get("origin") != origin:
                    continue
                if tier and e.get("tier") != tier:
                    continue
                if verdict and e.get("verdict") != verdict:
                    continue
                if from_ts and e.get("ts", "") < from_ts:
                    continue
                if to_ts and e.get("ts", "") > to_ts:
                    continue
                if report_id and e.get("report_id") != report_id:
                    continue
                if source and e.get("source") != source:
                    continue
                entries.append(e)
        return entries
