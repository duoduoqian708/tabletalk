"""安全闸门只读目录：规则元数据 + 当前配置（供前端规则面板渲染与配置）。"""
from __future__ import annotations

from fastapi import APIRouter

from app.safety.rules import RULE_META
from app.state import get_state

router = APIRouter(prefix="/api/v1", tags=["safety"])

_POLICY_KEYS = ("version", "table_rules", "pattern_rules", "threshold")


@router.get("/safety/rules")
async def safety_rules() -> dict:
    """规则目录：每条规则的 tier/作用域/默认判定/是否硬规则(floor)/当前覆盖。policy 现行配置一并返回。"""
    state = get_state()
    rt = state.runtime.get()
    cfg = rt.gate_rules or {}
    rules = [
        {
            "id": rid,
            "tier": meta.get("tier", "unknown"),
            "scope": meta.get("scope", ""),
            "default_verdict": meta.get("default", "review"),
            "floor": bool(meta.get("floor", False)),
            "override": cfg.get(rid),  # None=未覆盖（走默认）
        }
        for rid, meta in RULE_META.items()
    ]
    policy: dict | None = None
    try:
        from dataclasses import asdict

        p = asdict(rt.policy)
        if isinstance(p, dict):
            policy = {k: p[k] for k in _POLICY_KEYS if k in p}
    except Exception:  # noqa: BLE001
        policy = None
    return {"rules": rules, "policy": policy}