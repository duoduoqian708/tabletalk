"""L3 行为层：few-shot 库（T10）。

成功问答对（问题 → SQL + join 路径）入库；相似问题召回作为 in-context 样例。
存储：快照持久化（经门面 _load_conn/_save_conn，meta JSON）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("kb.behavior")

_MAX_PER_CONN = 500  # 每连接保留上限（T10 §5#6，可经 TABLETALK_FEWSHOT_MAX 配置）


def _max_per_conn() -> int:
    """每连接 few-shot 保留上限：env 优先（TABLETALK_FEWSHOT_MAX），缺省 500。"""
    import os
    try:
        return max(1, int(os.environ.get("TABLETALK_FEWSHOT_MAX", str(_MAX_PER_CONN))))
    except (TypeError, ValueError):
        return _MAX_PER_CONN

# v1 词面相似：问题 token 重叠打分；有 API 嵌入时升级为向量召回
_STOP = {"的", "了", "在", "是", "吗", "呢", "啊", "我", "你", "他", "把", "被", "和", "与", "或", "个"}


def _tokens(q: str) -> set[str]:
    """分词：英文词直接取；中文用 2-gram（连续中文整串会并成一个 token，需切分）。"""
    import re
    toks: set[str] = set()
    for m in re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]+", (q or "").lower()):
        if re.match(r"[a-z0-9_]", m):
            toks.add(m)
        else:
            for i in range(len(m) - 1):
                toks.add(m[i:i + 2])
    return toks - _STOP


class FewShotStore:
    def __init__(self) -> None:
        self._items: dict[str, list[dict]] = {}  # conn -> [{question, sql, join_path, created_at}]

    def add(self, conn_id: str, question: str, sql: str, join_path: list[str]) -> None:
        if not question or not sql:
            return
        from app.core.timeutil import utcnow_iso
        items = self._items.setdefault(conn_id, [])
        items.append({
            "question": question, "sql": sql, "join_path": join_path,
            "created_at": utcnow_iso(),
        })
        # 上限清理：保留最新
        cap = _max_per_conn()
        if len(items) > cap:
            items[:] = items[-cap:]
        logger.info("[behavior] few-shot：新增 1 条（conn=%s），当前 %d 条", conn_id, len(items))

    def recall(self, conn_id: str, question: str, k: int = 3) -> list[dict]:
        """相似问题召回（v1 词面相似）→ [{question, sql, join_path}]。"""
        items = self._items.get(conn_id, [])
        if not items:
            return []
        q_toks = _tokens(question)
        scored: list[tuple[float, dict]] = []
        for it in items:
            i_toks = _tokens(it["question"])
            if not q_toks or not i_toks:
                scored.append((0.0, it))
                continue
            score = len(q_toks & i_toks) / max(1.0, float(len(q_toks | i_toks)))
            if it["question"] == question:
                score += 1.0
            scored.append((score, it))
        scored.sort(key=lambda x: x[0], reverse=True)
        hits = [it for s, it in scored[:k] if s > 0.0]
        logger.info("[behavior] few-shot：召回 %d 条（conn=%s）", len(hits), conn_id)
        return hits

    def load(self, conn_id: str, items: list[dict]) -> None:
        self._items[conn_id] = list(items)

    def dump(self, conn_id: str) -> list[dict]:
        return list(self._items.get(conn_id, []))
