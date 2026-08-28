"""常用问题库 — SQLite 持久化（tabletalk.db: questions）。"""
from __future__ import annotations

import json
import re
import threading
import time
from app.core.timeutil import utcnow_iso
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.core.system_db import get_conn, init_system_db


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
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / "questions.json"
        self._lock = threading.Lock()
        init_system_db(self._data_dir)
        self._migrate_if_needed()

    def _migrate_if_needed(self):
        # 仅当 DB 空且旧文件存在时迁移
        try:
            con = get_conn(self._data_dir)
            cur = con.execute("SELECT COUNT(*) FROM questions")
            cnt = cur.fetchone()[0]
            con.close()
            if cnt > 0:
                return
        except Exception:
            pass
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            con = get_conn(self._data_dir)
            for conn_id, entries in data.items():
                for e in entries:
                    try:
                        con.execute(
                            "INSERT OR IGNORE INTO questions (id, conn_id, question, data) VALUES (?,?,?,?)",
                            (e["id"], conn_id, e["question"], json.dumps(e, ensure_ascii=False)),
                        )
                    except Exception:
                        continue
            con.commit()
            con.close()
            try:
                bak = self._path.with_suffix(".json.bak")
                if not bak.exists():
                    self._path.rename(bak)
            except OSError:
                pass
        except Exception:
            pass

    def list(self, conn_id: str) -> list[dict[str, Any]]:
        con = get_conn(self._data_dir)
        try:
            cur = con.execute("SELECT data FROM questions WHERE conn_id=?", (conn_id,))
            out = []
            for (data_json,) in cur.fetchall():
                try:
                    out.append(json.loads(data_json))
                except Exception:
                    continue
            return out
        finally:
            con.close()

    def save(self, conn_id: str, question: str, sql: str, tables: list[str] | None = None, visibility: str = "private") -> dict[str, Any]:
        q = (question or "").strip()
        s = (sql or "").strip()
        if not q or not s:
            raise ValueError("question and sql required")
        if visibility not in ("private", "public", "team"):
            visibility = "private"
        # T5.5 只收只读：问题库仅收 ALLOW 查询（DML/DDL 拒绝）
        try:
            from app.safety import gate as _gate
            from app.safety.models import Origin as _Origin
            from app.core.connections import ConnectionRegistry
            from app.state import get_state as _get_state
            # 取方言（失败回退 sqlite）
            dialect = "sqlite"
            try:
                st = _get_state()
                cfg = st.connections.get(conn_id)
                dialect = _gate.sqlglot_dialect_for(cfg.dialect)
            except Exception:
                pass
            assess = _gate.assess_sql(s, dialect, _Origin.AI)
            if assess.verdict.value != "allow":
                raise ValueError("问题库仅收只读查询")
        except ValueError:
            raise
        except Exception:
            # 评估异常时保守：不阻断保存（但 02 G6 要求仅只读，此处已尽力）
            pass
        entry = {
            "id": f"q_{int(time.time()*1000)}_{len(self.list(conn_id))}",
            "question": q,
            "sql": s,
            "tables": list(tables or []),
            "visibility": visibility,
            "created_at": utcnow_iso(),
            "hit_count": 0,
        }
        con = get_conn(self._data_dir)
        try:
            con.execute(
                "INSERT INTO questions (id, conn_id, question, data) VALUES (?,?,?,?)",
                (entry["id"], conn_id, q, json.dumps(entry, ensure_ascii=False)),
            )
            con.commit()
        finally:
            con.close()
        return entry

    def delete(self, conn_id: str, qid: str) -> bool:
        con = get_conn(self._data_dir)
        try:
            cur = con.execute("DELETE FROM questions WHERE conn_id=? AND id=?", (conn_id, qid))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()

    def _threshold(self) -> int:
        try:
            import os
            v = os.getenv("TABLETALK_QUESTION_THRESHOLD", "")
            if v:
                iv = int(v)
                if 0 < iv <= 100:
                    return iv
        except Exception:
            pass
        return 60

    def match(self, conn_id: str, question: str, tables: list[str] | None = None) -> dict[str, Any] | None:
        q = (question or "").strip().lower()
        if not q:
            return None
        def norm(s: str) -> str:
            return re.sub(r"[^\w\u4e00-\u9fff]+", "", s.lower())
        nq = norm(q)
        thr = self._threshold()
        with self._lock:
            candidates = self.list(conn_id)
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
                if score > best_score and score >= thr:
                    best_score = score
                    best = e
            if best:
                best["hit_count"] = int(best.get("hit_count", 0)) + 1
                # 更新 DB
                try:
                    con = get_conn(self._data_dir)
                    con.execute(
                        "UPDATE questions SET data=? WHERE id=?",
                        (json.dumps(best, ensure_ascii=False), best["id"]),
                    )
                    con.commit()
                    con.close()
                except Exception:
                    pass
            return best
