"""运行时可编辑配置（AI 网关 + 闸门参数），JSON 持久化。

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


@dataclass
class ModelConfig:
    """一个已配置的文本模型。"""
    id: str
    name: str
    provider: str = "mock"       # mock | cloud | local
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.2
    timeout: float = 120.0
    # 是否支持推理思考（None=未探测；测试连接时自动标定，可手动覆盖）
    reasoning: bool | None = None
    # 系统内置模型（开机自带，用户不可删除）
    builtin: bool = False
    # 探测结果（上次测试，可选）
    last_test: dict[str, Any] = field(default_factory=dict)


@dataclass
class EmbeddingModelConfig:
    """一个已配置的嵌入模型。"""
    id: str
    name: str
    provider: str = "hash"       # hash | api
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
    table_rules: dict[str, str] = field(default_factory=dict)  # table -> allow/review/block
    pattern_rules: list[dict[str, Any]] = field(default_factory=list)  # [{id, description, action}]
    threshold: int = 100000  # 影响行数阈值（默认 10 万）

@dataclass
class RuntimeSettings:
    # ---- 文本模型：多配置 + 默认 ----
    ai_models: list[ModelConfig] = field(default_factory=list)
    default_ai_model: str = ""
    # ---- 嵌入模型：多配置 + 默认 ----
    embedding_models: list[EmbeddingModelConfig] = field(default_factory=list)
    default_embedding_model: str = ""
    # ---- 闸门参数 ----
    gate_review_threshold: int = 1000
    gate_rules: dict[str, bool] = field(default_factory=dict)
    policy: Policy = field(default_factory=Policy)
    # ---- 知识库 ----
    kb_sample_rows: int = 10
    kb_ai_annotation_samples: bool = False
    kb_sync_minutes: int = 30   # 知识库增量同步周期（分钟；0=关闭）
    # ---- 隐私三档（B4） ----
    privacy_mode: str = "standard"  # strict | standard | open
    # ---- 查询 / 连接池（运行时可覆盖 env 默认值）----
    query_max_rows: int = 1000
    pool_size: int = 3

    # ---- 兼容层：旧单组字段的 property ----
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
        return ModelConfig(id="default-mock", name="Mock", provider="mock")

    def _default_embedding(self) -> EmbeddingModelConfig:
        if self.embedding_models:
            for m in self.embedding_models:
                if m.id == self.default_embedding_model:
                    return m
            return self.embedding_models[0]
        return EmbeddingModelConfig(id="default-hash", name="Hash（离线）", provider="hash")

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
            "gate_review_threshold": self.gate_review_threshold,
            "gate_rules": self.gate_rules,
            "policy": asdict(self.policy),
            "kb_sample_rows": self.kb_sample_rows,
            "kb_ai_annotation_samples": self.kb_ai_annotation_samples,
            "kb_sync_minutes": self.kb_sync_minutes,
            "privacy_mode": self.privacy_mode,
            "query_max_rows": self.query_max_rows,
            "pool_size": self.pool_size,
            "runtime": {
                "data_dir": str(get_env().data_dir),
                "port": get_env().port,
                "auth": "X-TableTalk-Token · 本机",
            },
            # 兼容字段（旧前端 / 旧脚本仍能读）
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
            "reasoning": m.reasoning,
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
    "gate_review_threshold",
    "gate_rules",
    "policy",
    "kb_sample_rows",
    "kb_ai_annotation_samples",
    "privacy_mode",
    "query_max_rows",
    "pool_size",
    # 旧字段兼容
    "ai_provider", "ai_base_url", "ai_api_key", "ai_model",
    "ai_temperature", "ai_timeout",
    "embedding_provider", "embedding_base_url", "embedding_api_key", "embedding_model",
}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _migrate_from_legacy(data: dict[str, Any]) -> None:
    """如果有旧单组字段但完全没有 ai_models 键，自动迁移为列表第一条。不注入任何内置模型。
    注意：ai_models=[] 表示"已配置、暂无模型"（用户清空），不算缺失，不触发迁移。"""
    if "ai_models" not in data or data.get("ai_models") is None:
        m = ModelConfig(
            id=_new_id("llm"),
            name="默认模型",
            provider=data.get("ai_provider", "mock"),
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
            provider=data.get("embedding_provider", "hash"),
            base_url=data.get("embedding_base_url", ""),
            api_key=data.get("embedding_api_key", ""),
            model=data.get("embedding_model", "bge-m3"),
        )
        data["embedding_models"] = [asdict(m)]
        data["default_embedding_model"] = m.id


def _mask_key(key: str | None) -> str:
    """API key 脱敏展示：保留开头 / 结尾各 3 位，中间以 ••• 代替，可辨识但不可还原。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "•••"
    return f"{key[:3]}•••{key[-3:]}"


def _is_masked(val: Any) -> bool:
    return isinstance(val, str) and "•••" in val


def _restore_masked_keys(cur: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """前端读到的 api_key 是掩码（含 •••）；全量写回时把掩码还原为旧真值，避免覆盖 key。"""
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
        self._path = data_dir / "settings.json"
        env = get_env()
        self._data: dict[str, Any] = {
            "gate_review_threshold": env.gate_review_threshold,
            "gate_rules": {},
            "policy": asdict(Policy()),
            "kb_sample_rows": env.kb_sample_rows,
            "kb_ai_annotation_samples": env.kb_ai_annotation_samples,
            "privacy_mode": "standard",
            "query_max_rows": env.query_max_rows,
            "pool_size": env.pool_size,
        }
        # 从 env 初始化默认模型（有 env 配置才有首条；否则空列表，等用户自己配）
        if env.ai_provider != "mock" and env.ai_model:
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

        if env.embedding_provider != "hash":
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
            # 离线兜底由知识库内部 HashingEmbedder 提供，UI 不再暴露 Hash 向量模型
            self._data["embedding_models"] = []
            self._data["default_embedding_model"] = ""

        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
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
            # 加载时剔除了遗留内置模型 → 顺手落盘，把 settings.json 也清干净
            self.save()

    def _ensure_builtins(self) -> bool:
        """不再内置任何模型。这里只做收尾：
        ① 剔除历史遗留的 builtin 标记模型（如旧的 llm_deepseek / llm_mock，用不起来）；
        ② 剔除离线 Hash 向量模型（知识库内部仍有 HashingEmbedder 兜底，UI 不再暴露）；
        ③ default_ai_model / default_embedding_model 指向不存在 / 空时回退到列表第一条（空则留空）。
        返回是否有剔除发生（调用方可据此落盘）。"""
        removed = False
        models = self._data["ai_models"]
        # ① 剔除遗留内置项
        kept = [m for m in models if not m.get("builtin")]
        if len(kept) != len(models):
            removed = True
        self._data["ai_models"] = kept

        # ② 剔除离线 Hash 向量模型
        emb = self._data.get("embedding_models", [])
        emb_kept = [m for m in emb if m.get("provider") != "hash"]
        if len(emb_kept) != len(emb):
            removed = True
        self._data["embedding_models"] = emb_kept

        # ③ 默认模型校正（文本）
        cur = self._data["ai_models"]
        cur_ids = {m.get("id") for m in cur}
        default = self._data.get("default_ai_model", "")
        if not default or default not in cur_ids:
            self._data["default_ai_model"] = cur[0]["id"] if cur else ""

        # ③ 默认模型校正（向量）
        emb_ids = {m.get("id") for m in emb_kept}
        emb_def = self._data.get("default_embedding_model", "")
        if not emb_def or emb_def not in emb_ids:
            self._data["default_embedding_model"] = emb_kept[0]["id"] if emb_kept else ""

        return removed

    def save(self) -> None:
        with self._lock:
            out = {k: v for k, v in self._data.items() if k in _PERSISTED_KEYS}
            self._path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                self._path.chmod(0o600)
            except OSError:
                pass

    def get(self) -> RuntimeSettings:
        data = dict(self._data)
        _migrate_from_legacy(data)
        ai_models = [ModelConfig(**m) for m in data.get("ai_models", [])]
        emb_models = [EmbeddingModelConfig(**m) for m in data.get("embedding_models", [])]
        pm = data.get("privacy_mode", "standard")
        if pm not in ("strict", "standard", "open"):
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
            gate_review_threshold=data.get("gate_review_threshold", 1000),
            gate_rules=data.get("gate_rules", {}),
            policy=policy,
            kb_sample_rows=data.get("kb_sample_rows", 10),
            kb_ai_annotation_samples=data.get("kb_ai_annotation_samples", False),
            privacy_mode=pm,
            query_max_rows=data.get("query_max_rows", 1000),
            pool_size=data.get("pool_size", 3),
        )

    def update(self, patch: dict[str, Any]) -> RuntimeSettings:
        """部分更新。支持两种格式：
        - 新格式：传 ai_models / default_ai_model / embedding_models / default_embedding_model
        - 旧格式：传 ai_provider / ai_base_url / ...（更新默认模型的对应字段）
        """
        with self._lock:
            # 先确保有列表结构（key 缺失才迁移；空列表 = 用户清空，不算缺失）
            if "ai_models" not in self._data or self._data.get("ai_models") is None:
                _migrate_from_legacy(self._data)
            if "embedding_models" not in self._data or self._data.get("embedding_models") is None:
                _migrate_from_legacy(self._data)

            # 新格式：直接替换列表（api_key 掩码 "•••" 不回写，保留旧值）
            if "ai_models" in patch:
                self._data["ai_models"] = _restore_masked_keys(self._data["ai_models"], patch["ai_models"])
            if "default_ai_model" in patch:
                self._data["default_ai_model"] = patch["default_ai_model"]
            if "embedding_models" in patch:
                self._data["embedding_models"] = _restore_masked_keys(self._data["embedding_models"], patch["embedding_models"])
            if "default_embedding_model" in patch:
                self._data["default_embedding_model"] = patch["default_embedding_model"]

            # 旧格式兼容：更新默认模型
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

            # 其他字段
            for k in ("gate_review_threshold", "gate_rules",
                      "kb_sample_rows", "kb_ai_annotation_samples",
                      "privacy_mode",
                      "query_max_rows", "pool_size"):
                if k in patch:
                    v = patch[k]
                    if k == "privacy_mode":
                        if v not in ("strict", "standard", "open"):
                            continue
                    if k in ("query_max_rows", "pool_size"):
                        # 防御：非整数 / 非正整数不写入，保留既有值（env 默认值）
                        try:
                            vi = int(v)
                        except (TypeError, ValueError):
                            continue
                        if vi < 1:
                            continue
                        v = vi
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
                    # 同步 gate_review_threshold 与 policy.threshold
                    if "threshold" in pol:
                        try:
                            thr = int(pol["threshold"])
                            if thr >= 1:
                                self._data["gate_review_threshold"] = thr
                        except (TypeError, ValueError):
                            pass
                    self._data["policy"] = pol

        self._ensure_builtins()
        self.save()
        return self.get()
