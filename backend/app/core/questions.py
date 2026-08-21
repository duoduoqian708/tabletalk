"""常用问题库 — C6 复利资产（个人版 data_dir 存储，P3 团队化）。

存储：data_dir/questions.json（每连接一套，含 question/sql/tables/tags）
命中：新提问先走本地问题库匹配（问题文本相似 + 表集合匹配），命中直接执行免模型调用
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

@dataclass
class QuestionEntry:
    id: str
    question: str
    sql: str
    tables: list[str] = field(default_factory=list)
    created_at: str = ""
    hit_count: int = 0

class QuestionStore:
    def __init__(self, data_dir: Path):
        self._path = data_dir / "questions.json"
        self._lock = threading.Lock()
        self._data: dict[str, list[dict[str, Any]]] = {}  # conn_id -> entries
        self._load()

    def _load(self):
        if not self._path.exists():
            return
        try:
            self._data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            self._data = {}

    def _save(self):
        with self._lock:
            self._path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                self._path.chmod(0o600)
            except OSError:
                pass

    def list(self, conn_id: str) -> list[dict[str, Any]]:
        return list(self._data.get(conn_id, []))

    def save(self, conn_id: str, question: str, sql: str, tables: list[str] | None = None) -> dict[str, Any]:
        q = (question or "").strip()
        s = (sql or "").strip()
        if not q or not s:
            raise ValueError("question and sql required")
        entry = {
            "id": f"q_{int(time.time()*1000)}_{len(self._data.get(conn_id, []))}",
            "question": q,
            "sql": s,
            "tables": list(tables or []),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "hit_count": 0,
        }
        self._data.setdefault(conn_id, []).append(entry)
        self._save()
        return entry

    def delete(self, conn_id: str, qid: str) -> bool:
        lst = self._data.get(conn_id, [])
        new = [e for e in lst if e["id"] != qid]
        if len(new) == len(lst):
            return False
        self._data[conn_id] = new
        self._save()
        return True

    def match(self, conn_id: str, question: str, tables: list[str] | None = None) -> dict[str, Any] | None:
        """本地匹配：问题文本相似（归一化后包含）+ 表集合重叠（若提供）。"""
        q = (question or "").strip().lower()
        if not q:
            return None
        def norm(s: str) -> str:
            return re.sub(r"[^\w\u4e00-\u9fff]+", "", s.lower())
        nq = norm(q)
        with self._lock:
            candidates = list(self._data.get(conn_id, []))
            best = None
            best_score = 0
            for e in candidates:
                eq = norm(e["question"])
                score = 0
                if nq == eq:
                    score = 100
                elif nq in eq or eq in nq:
                    score = 80
                elif len(nq) > 4 and len(eq) > 4 and (nq[:6] == eq[:6] or nq[-6:] == eq[-6:]):
                    score = 60
                if tables and e.get("tables"):
                    overlap = len(set(t.lower() for t in tables) & set(t.lower() for t in e["tables"]))
                    if overlap:
                        score += overlap * 10
                if score > best_score and score >= 60:
                    best_score = score
                    best = e
            if best:
                best["hit_count"] = int(best.get("hit_count", 0)) + 1
                # 直接写盘（已在锁内，避免 _save 的二次加锁死锁，改为内联）
                self._path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
                try:
                    self._path.chmod(0o600)
                except OSError:
                    pass
            return best
