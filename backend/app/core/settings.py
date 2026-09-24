"""运行时可编辑配置（AI 网关 + 闸门参数），SQLite 持久化（tabletalk.db: system_kv）。

进程级 env 配置（端口/数据目录/默认值）见 app.config；这里是从 env 默认值出发、
可被 PUT /settings 覆盖的运行时配置。

模型配置演进：从单组 ai_* 字段升级为 ai_models 列表（多模型管理）。
旧字段保留为读写兼容：读取时如果存在 ai_* 但没有 ai_models，自动迁移为第一条；
写入时 PUT /settings 仍接受 ai_* 字段（更新默认模型）和 ai_models（全量替换列表）。
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.config import get_env
from app.core.system_db import get_conn, init_system_db


@dataclass
class ModelConfig:
    """一个已配置的文本模型。"""
    id: str
    name: str
    provider: str = "mock"       # 供应商名（如 deepseek / glm / 火山方舟 / qwen）；mock = 内置模拟
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.2
    timeout: float = 120.0
    reasoning: bool | None = None
    builtin: bool = False
    last_test: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] | None = None  # /ai/test 探测落库：{reasoning, reasoning_effort}


@dataclass
class EmbeddingModelConfig:
    """一个已配置的嵌入模型。"""
    id: str
    name: str
    provider: str = ""            # 向量模型供应商（自由文本，如 openai/deepseek/…）；空 = 未配置
    base_url: str = ""
    api_key: str = ""
    model: str = "bge-m3"
    dimensions: int | None = None
    last_test: dict[str, Any] = field(default_factory=dict)


@dataclass
class Policy:
    version: int = 1
    updated_by: str = "system"
    updated_at: str = ""
    table_rules: dict[str, str] = field(default_factory=dict)
    pattern_rules: list[dict[str, Any]] = field(default_factory=list)
    threshold: int = 100000


@dataclass
class RuntimeSettings:
    ai_models: list[ModelConfig] = field(default_factory=list)
    default_ai_model: str = ""
    embedding_models: list[EmbeddingModelConfig] = field(default_factory=list)
    default_embedding_model: str = ""
    gate_rules: dict[str, bool] = field(default_factory=dict)
    policy: Policy = field(default_factory=Policy)
    kb_sample_rows: int = 15
    kb_ai_annotation_samples: bool = False
    kb_build_reasoning_effort: str = "low"  # KB 阶段3 推理档位（off/low/medium/high；默认浅推理 low，不搞深度）
    kb_sync_minutes: int = 30
    privacy_mode: str = "standard"
    query_max_rows: int = 1000
    pool_size: int = 3
    default_connection: str = ""   # 默认数据源（连接 id）：后端持久化，跨浏览器/origin 一致；空=未设置

    @property
    def ai_provider(self) -> str:
        return self._default_ai().provider

    @property
    def ai_base_url(self) -> str:
        return self._default_ai().base_url

    @property
    def ai_api_key(self) -> str:
        return self._default_ai().api_key

    @property
    def ai_model(self) -> str:
        return self._default_ai().model

    @property
    def ai_temperature(self) -> float:
        return self._default_ai().temperature

    @property
    def ai_timeout(self) -> float:
        return self._default_ai().timeout

    @property
    def embedding_provider(self) -> str:
        return self._default_embedding().provider

    @property
    def embedding_base_url(self) -> str:
        return self._default_embedding().base_url

    @property
    def embedding_api_key(self) -> str:
        return self._default_embedding().api_key

    @property
    def embedding_model(self) -> str:
        return self._default_embedding().model

    def _default_ai(self) -> ModelConfig:
        if self.ai_models:
            for m in self.ai_models:
                if m.id == self.default_ai_model:
                    return m
            return self.ai_models[0]
        return ModelConfig(id="", name="未配置", provider="")  # 未配置 → AI 不可用

    def _default_embedding(self) -> EmbeddingModelConfig:
        if self.embedding_models:
            for m in self.embedding_models:
                if m.id == self.default_embedding_model:
                    return m
            return self.embedding_models[0]
        return EmbeddingModelConfig(id="default-embedding", name="向量模型（未配置）", provider="")

    def public(self) -> dict[str, Any]:
        d = {
            "ai_models": [
                {**asdict(m), "api_key": _mask_key(m.api_key)}
                for m in self.ai_models
            ],
            "default_ai_model": self.default_ai_model,
            "embedding_models": [
                {**asdict(m), "api_key": _mask_key(m.api_key)}
                for m in self.embedding_models
            ],
            "default_embedding_model": self.default_embedding_model,
            "gate_rules": self.gate_rules,
            "policy": asdict(self.policy),
            "kb_sample_rows": self.kb_sample_rows,
            "kb_ai_annotation_samples": self.kb_ai_annotation_samples,
            "kb_build_reasoning_effort": self.kb_build_reasoning_effort,
            "kb_sync_minutes": self.kb_sync_minutes,
            "privacy_mode": self.privacy_mode,
            "query_max_rows": self.query_max_rows,
            "pool_size": self.pool_size,
            "default_connection": self.default_connection,
            "runtime": {
                "data_dir": str(get_env().data_dir),
                "port": get_env().port,
                "auth": "X-TableTalk-Token · 本机",
            },
            "ai_provider": self.ai_provider,
            "ai_base_url": self.ai_base_url,
            "ai_api_key": _mask_key(self.ai_api_key),
            "ai_model": self.ai_model,
            "ai_temperature": self.ai_temperature,
            "ai_timeout": self.ai_timeout,
            "embedding_provider": self.embedding_provider,
            "embedding_base_url": self.embedding_base_url,
            "embedding_api_key": _mask_key(self.embedding_api_key),
            "embedding_model": self.embedding_model,
        }
        return d

    def provider_config(self) -> dict[str, Any]:
        m = self._default_ai()
        return {
            "provider": m.provider,
            "base_url": m.base_url,
            "api_key": m.api_key,
            "model": m.model,
            "temperature": m.temperature,
            "timeout": m.timeout,
            "reasoning": reasoning_for_model(m),
        }

    def embedding_config(self) -> dict[str, Any]:
        m = self._default_embedding()
        return {
            "provider": m.provider,
            "base_url": m.base_url,
            "api_key": m.api_key,
            "model": m.model,
            "dimensions": m.dimensions,
        }


_PERSISTED_KEYS = {
    "ai_models",
    "default_ai_model",
    "embedding_models",
    "default_embedding_model",
    "gate_rules",
    "policy",
    "kb_sample_rows",
    "kb_ai_annotation_samples",
    "kb_build_reasoning_effort",
    "privacy_mode",
    "query_max_rows",
    "pool_size",
    "default_connection",
    "ai_provider", "ai_base_url", "ai_api_key", "ai_model",
    "ai_temperature", "ai_timeout",
    "embedding_provider", "embedding_base_url", "embedding_api_key", "embedding_model",
}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _migrate_from_legacy(data: dict[str, Any]) -> None:
    if "ai_models" not in data or data.get("ai_models") is None:
        m = ModelConfig(
            id=_new_id("llm"),
            name="默认模型",
            provider=data.get("ai_provider", ""),
            base_url=data.get("ai_base_url", ""),
            api_key=data.get("ai_api_key", ""),
            model=data.get("ai_model", ""),
            temperature=data.get("ai_temperature", 0.2),
            timeout=data.get("ai_timeout", 120.0),
        )
        data["ai_models"] = [asdict(m)]
        data["default_ai_model"] = m.id

    if "embedding_models" not in data or data.get("embedding_models") is None:
        m = EmbeddingModelConfig(
            id=_new_id("emb"),
            name="默认嵌入",
            provider=data.get("embedding_provider", ""),
            base_url=data.get("embedding_base_url", ""),
            api_key=data.get("embedding_api_key", ""),
            model=data.get("embedding_model", "bge-m3"),
        )
        data["embedding_models"] = [asdict(m)]
        data["default_embedding_model"] = m.id


def _mask_key(key: str | None) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•••"
    return f"{key[:3]}•••{key[-3:]}"


# gate_rules 契约（严格度阶梯）与引擎共用单一来源：normalize_gate_rules
def _clean_gate_rules(rules: object) -> dict[str, str]:
    from app.safety.rules import normalize_gate_rules

    return normalize_gate_rules(rules)


def reasoning_for_model(m: "ModelConfig") -> str | None:
    """按模型探测能力归一推理强度（避免把 True 盲目当 high 导致 400）。

    deepseek/glm 等模型探测出「支持思考」但 reasoning_effort 全档位 400 时，
    只存 capability 不存档位（reasoning_effort=None）——此时返回 None，不追加参数，保正常可用。
    显式档位（low/medium/high）或探测出有效档位才返回该档。
    """
    r = m.reasoning
    # 显式档位最优先
    if isinstance(r, str) and r in ("low", "medium", "high"):
        return r
    caps = m.capabilities or {}
    cap_effort = caps.get("reasoning_effort")
    if caps.get("reasoning") and isinstance(cap_effort, str) and cap_effort in ("low", "medium", "high"):
        return cap_effort
    # 仅 bool True 且探测不出可用档位 → 不推理，保连通
    return None


def _is_masked(val: Any) -> bool:
    return isinstance(val, str) and "•••" in val


def _restore_masked_keys(cur: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    real_by_id = {m.get("id"): m.get("api_key", "") for m in cur}
    out = []
    for m in incoming:
        m = dict(m)
        if _is_masked(m.get("api_key")) and m.get("id") in real_by_id:
            m["api_key"] = real_by_id[m["id"]]
        out.append(m)
    return out


class SettingsStore:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / "settings.json"  # 旧文件，仅迁移
        env = get_env()
        self._data: dict[str, Any] = {
            "gate_rules": {},
            "policy": asdict(Policy()),
            "kb_sample_rows": env.kb_sample_rows,
            "kb_ai_annotation_samples": env.kb_ai_annotation_samples,
            "kb_build_reasoning_effort": "low",
            "privacy_mode": "standard",
            "query_max_rows": env.query_max_rows,
            "pool_size": env.pool_size,
        }
        if env.ai_provider and env.ai_model:
            env_ai = ModelConfig(
                id=_new_id("llm"),
                name="环境配置模型",
                provider=env.ai_provider,
                base_url=env.ai_base_url,
                api_key=env.ai_api_key,
                model=env.ai_model,
                temperature=env.ai_temperature,
                timeout=env.ai_timeout,
                reasoning=env.ai_reasoning,
            )
            self._data["ai_models"] = [asdict(env_ai)]
            self._data["default_ai_model"] = env_ai.id
        else:
            self._data["ai_models"] = []
            self._data["default_ai_model"] = ""

        if env.embedding_provider:
            env_emb = EmbeddingModelConfig(
                id=_new_id("emb"),
                name="环境配置嵌入",
                provider=env.embedding_provider,
                base_url=env.embedding_base_url,
                api_key=env.embedding_api_key,
                model=env.embedding_model,
            )
            self._data["embedding_models"] = [asdict(env_emb)]
            self._data["default_embedding_model"] = env_emb.id
        else:
            self._data["embedding_models"] = []
            self._data["default_embedding_model"] = ""

        self._lock = threading.Lock()
        init_system_db(self._data_dir)
        self._load()

    def _load(self) -> None:
        # 优先从 DB 读
        try:
            con = get_conn(self._data_dir)
            cur = con.execute("SELECT v FROM system_kv WHERE k='settings'")
            row = cur.fetchone()
            con.close()
            if row:
                data = json.loads(row[0])
                _migrate_from_legacy(data)
                for k in _PERSISTED_KEYS:
                    if k in data:
                        self._data[k] = data[k]
                if self._ensure_builtins():
                    self.save()
                return
        except Exception:
            pass
        # 回退：旧 JSON 迁移
        if not self._path.exists():
            self._ensure_builtins()
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            _migrate_from_legacy(data)
            for k in _PERSISTED_KEYS:
                if k in data:
                    self._data[k] = data[k]
        except Exception:
            pass
        if self._ensure_builtins():
            self.save()
        else:
            # 首次从 JSON 迁移后落库
            self.save()

    def _ensure_builtins(self) -> bool:
        removed = False
        models = self._data["ai_models"]
        kept = [m for m in models if not m.get("builtin")]
        if len(kept) != len(models):
            removed = True
        self._data["ai_models"] = kept
        emb = self._data.get("embedding_models", [])
        emb_kept = [m for m in emb if m.get("provider") or m.get("base_url")]  # 保留有真实配置的
        if len(emb_kept) != len(emb):
            removed = True
        self._data["embedding_models"] = emb_kept
        cur = self._data["ai_models"]
        cur_ids = {m.get("id") for m in cur}
        default = self._data.get("default_ai_model", "")
        if not default or default not in cur_ids:
            self._data["default_ai_model"] = cur[0]["id"] if cur else ""
        emb_ids = {m.get("id") for m in emb_kept}
        emb_def = self._data.get("default_embedding_model", "")
        if not emb_def or emb_def not in emb_ids:
            self._data["default_embedding_model"] = emb_kept[0]["id"] if emb_kept else ""
        return removed

    def save(self) -> None:
        with self._lock:
            out = {k: v for k, v in self._data.items() if k in _PERSISTED_KEYS}
            # 写 DB
            try:
                con = get_conn(self._data_dir)
                con.execute(
                    "INSERT OR REPLACE INTO system_kv (k, v) VALUES (?, ?)",
                    ("settings", json.dumps(out, ensure_ascii=False)),
                )
                con.commit()
                con.close()
            except Exception:
                pass
            # 兼容：旧文件归档（若存在则重命名）
            if self._path.exists():
                try:
                    bak = self._path.with_suffix(".json.bak")
                    if not bak.exists():
                        self._path.rename(bak)
                except OSError:
                    pass

    def get(self) -> RuntimeSettings:
        data = dict(self._data)
        _migrate_from_legacy(data)
        # 反序列化容错（X1）：按 dataclass 字段白名单过滤，手工编辑配置多写键不崩溃（与 Policy 同风格）
        ai_models = [
            ModelConfig(**{k: v for k, v in m.items() if k in ModelConfig.__dataclass_fields__})
            for m in data.get("ai_models", [])
        ]
        emb_models = [
            EmbeddingModelConfig(**{k: v for k, v in m.items() if k in EmbeddingModelConfig.__dataclass_fields__})
            for m in data.get("embedding_models", [])
        ]
        # 清理已废弃的 hash 嵌入模型（离线哈希已移除，向量模型只能通过 API 接入）
        emb_models = [m for m in emb_models if m.provider != "hash"]
        pm = data.get("privacy_mode", "standard")
        if pm not in ("strict", "standard", "open", "custom"):
            pm = "standard"
        pol_data = data.get("policy", {})
        if isinstance(pol_data, dict):
            try:
                policy = Policy(**{k: v for k, v in pol_data.items() if k in Policy.__dataclass_fields__})
            except Exception:
                policy = Policy()
        else:
            policy = Policy()
        return RuntimeSettings(
            ai_models=ai_models,
            default_ai_model=data.get("default_ai_model", ""),
            embedding_models=emb_models,
            default_embedding_model=data.get("default_embedding_model", ""),
            gate_rules=data.get("gate_rules", {}),
            policy=policy,
            kb_sample_rows=data.get("kb_sample_rows", 15),
            kb_ai_annotation_samples=data.get("kb_ai_annotation_samples", False),
            kb_build_reasoning_effort=data.get("kb_build_reasoning_effort", "low"),
            privacy_mode=pm,
            query_max_rows=data.get("query_max_rows", 1000),
            pool_size=data.get("pool_size", 3),
            default_connection=data.get("default_connection", ""),
        )

    def update(self, patch: dict[str, Any]) -> RuntimeSettings:
        with self._lock:
            if "ai_models" not in self._data or self._data.get("ai_models") is None:
                _migrate_from_legacy(self._data)
            if "embedding_models" not in self._data or self._data.get("embedding_models") is None:
                _migrate_from_legacy(self._data)
            if "ai_models" in patch:
                self._data["ai_models"] = _restore_masked_keys(self._data["ai_models"], patch["ai_models"])
            if "default_ai_model" in patch:
                self._data["default_ai_model"] = patch["default_ai_model"]
            if "embedding_models" in patch:
                self._data["embedding_models"] = _restore_masked_keys(self._data["embedding_models"], patch["embedding_models"])
            if "default_embedding_model" in patch:
                self._data["default_embedding_model"] = patch["default_embedding_model"]
            ai_legacy_fields = {"ai_provider": "provider", "ai_base_url": "base_url",
                                "ai_api_key": "api_key", "ai_model": "model",
                                "ai_temperature": "temperature", "ai_timeout": "timeout"}
            if any(k in patch for k in ai_legacy_fields):
                default_id = self._data.get("default_ai_model", "")
                models = self._data["ai_models"]
                target = next((m for m in models if m["id"] == default_id), models[0] if models else None)
                if target is None:
                    m = ModelConfig(id=_new_id("llm"), name="默认模型")
                    models.append(asdict(m))
                    target = models[-1]
                    self._data["default_ai_model"] = target["id"]
                for legacy_k, model_k in ai_legacy_fields.items():
                    if legacy_k in patch:
                        target[model_k] = patch[legacy_k]
            emb_legacy_fields = {"embedding_provider": "provider", "embedding_base_url": "base_url",
                                 "embedding_api_key": "api_key", "embedding_model": "model"}
            if any(k in patch for k in emb_legacy_fields):
                default_id = self._data.get("default_embedding_model", "")
                models = self._data["embedding_models"]
                target = next((m for m in models if m["id"] == default_id), models[0] if models else None)
                if target is None:
                    m = EmbeddingModelConfig(id=_new_id("emb"), name="默认嵌入")
                    models.append(asdict(m))
                    target = models[-1]
                    self._data["default_embedding_model"] = target["id"]
                for legacy_k, model_k in emb_legacy_fields.items():
                    if legacy_k in patch:
                        target[model_k] = patch[legacy_k]
            for k in ("gate_rules",
                      "kb_sample_rows", "kb_ai_annotation_samples",
                      "kb_build_reasoning_effort",
                      "kb_sync_minutes",
                      "privacy_mode",
                      "query_max_rows", "pool_size",
                      "default_connection"):
                if k in patch:
                    v = patch[k]
                    if k == "privacy_mode":
                        if v not in ("strict", "standard", "open", "custom"):
                            continue
                    if k == "kb_build_reasoning_effort":
                        if v not in ("off", "low", "medium", "high"):
                            continue
                        v = str(v)
                    if k == "default_connection":
                        v = str(v or "").strip()  # 空=清除默认
                    if k in ("query_max_rows", "pool_size", "kb_sync_minutes"):
                        try:
                            vi = int(v)
                        except (TypeError, ValueError):
                            continue
                        # kb_sync_minutes=0 允许（关闭自动同步）；其余 ≥1
                        if vi < 1 and not (k == "kb_sync_minutes" and vi == 0):
                            continue
                        v = vi
                    if k == "gate_rules":
                        v = _clean_gate_rules(v)
                    self._data[k] = v
            if "policy" in patch:
                pol = patch["policy"]
                if isinstance(pol, dict):
                    cur = self._data.get("policy", {})
                    if isinstance(cur, dict):
                        pol = {**pol}
                        pol["version"] = int(cur.get("version", 0)) + 1
                    else:
                        pol["version"] = 1
                    pol["updated_at"] = __import__("time").strftime("%Y-%m-%dT%H:%M:%S")
                    if "updated_by" not in pol or not pol["updated_by"]:
                        pol["updated_by"] = "api"
                    self._data["policy"] = pol
        self._ensure_builtins()
        self.save()
        return self.get()
