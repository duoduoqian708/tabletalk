"""技能模型：Skill / ScriptSpec / ScriptStep。

Skill 是"使用指导书"——一段可配置的流程描述，把若干原子 Tool 按剧本串起来，
并携带自己的 system prompt、触发词与只读等元数据。这是可插拔、可增删的基础。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class ScriptStep:
    """剧本的一步：调用哪个工具（可为 None=内部原语）、输入来源、产物、约束。"""
    id: str
    label: str                                # 前端展示名，如 "意图分解"
    tool: str | None = None                   # 本步调用的原子 Tool 名
    input_from: str | None = None             # 输入来源（上一步产物字段）
    output: str | None = None                 # 本步产物字段名
    constraint: str | None = None             # 安全/流程约束


@dataclass
class ScriptSpec:
    """技能剧本：步骤序列 + 是否需要 schema/审计等上下文。"""
    steps: list[ScriptStep] = field(default_factory=list)
    requires_schema: bool = False
    requires_history: bool = False


@dataclass
class Skill:
    """一个技能 = 使用指导书。id 唯一；tools 为本剧本可启用的原子工具名。"""
    id: str
    name: str
    description: str                          # 意图识别用的能力描述
    tools: list[str] = field(default_factory=list)
    script: ScriptSpec | None = None
    system_prompt: str = ""
    builtin: bool = False
    read_only: bool = False
    enabled: bool = True                      # 启用/禁用（技能广场）
    triggers: list[str] = field(default_factory=list)  # 关键词路由（自定义技能）
    # E5/§15：路由匹配条件（action/modality 剖面）+ 循环控制 + 降级策略
    match: dict = field(default_factory=dict)            # {action: ..., modality: ...}（None=任意）
    termination: dict = field(default_factory=dict)      # {max_turns, done_when}
    degradation: str = ""                                # 能力降级说明（路由命中但降级时提示）

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.script is not None:
            d["script"] = asdict(self.script)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Skill":
        script = d.get("script")
        spec = None
        if script:
            spec = ScriptSpec(
                steps=[ScriptStep(**s) for s in script.get("steps", [])],
                requires_schema=script.get("requires_schema", False),
                requires_history=script.get("requires_history", False),
            )
        return cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            description=d.get("description", ""),
            tools=list(d.get("tools", [])),
            script=spec,
            system_prompt=d.get("system_prompt", ""),
            builtin=bool(d.get("builtin", False)),
            read_only=bool(d.get("read_only", False)),
            enabled=bool(d.get("enabled", True)),
            triggers=list(d.get("triggers", [])),
            match=dict(d.get("match") or {}),
            termination=dict(d.get("termination") or {}),
            degradation=str(d.get("degradation") or ""),
        )
