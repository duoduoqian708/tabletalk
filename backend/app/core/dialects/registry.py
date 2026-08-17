"""方言注册表：name -> DialectAdapter 类。新增数据库只需注册。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.dialects.base import DialectAdapter


class DialectRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, type[DialectAdapter]] = {}

    def register(self, adapter_cls: type[DialectAdapter]) -> None:
        self._adapters[adapter_cls.name] = adapter_cls

    def get(self, name: str) -> DialectAdapter:
        cls = self._adapters.get(name)
        if cls is None:
            raise ValueError(
                f"不支持的数据库方言: {name!r}，已注册: {sorted(self._adapters)}"
            )
        return cls()

    def has(self, name: str) -> bool:
        return name in self._adapters

    def names(self) -> list[str]:
        return sorted(self._adapters)


registry = DialectRegistry()


def get_dialect(name: str) -> DialectAdapter:
    return registry.get(name)


def register_dialect(cls: type[DialectAdapter]) -> type[DialectAdapter]:
    registry.register(cls)
    return cls
