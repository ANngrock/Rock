"""Реестр инструментов: Pydantic-модель аргументов → OpenAI function schema.

Инструмент = описание + схема + handler + метаданные риска. Метаданные (``writes``, ``risk``)
существуют ради policy engine: регистрация инструмента уже говорит, надо ли его подтверждать.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from aegis.governance.policy import Risk, Trust

__all__ = ["Attachment", "ToolContext", "ToolRegistry", "ToolSpec", "UnknownTool", "registry"]

# OpenAI name: ^[a-zA-Z0-9_-]{1,64}$; у нас дополнительно snake_case
_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class UnknownTool(KeyError):
    """Модель попросила инструмент, которого нет в реестре."""


@dataclass(slots=True)
class Attachment:
    """Байты, присланные владельцем (фото/файл), уже скачанные из Telegram."""

    data: bytes
    mime: str = "image/jpeg"
    kind: str = "image"
    name: str | None = None


@dataclass(slots=True)
class ToolContext:
    """Всё, что нужно handler'у, кроме аргументов: трасса, владелец, доверие, сервисы."""

    trace_id: str
    owner_id: int
    services: Any = None  # aegis.agents.services.Services (Any — чтобы не циклировать импорт)
    source_trust: Trust = "owner"
    attachments: list[Attachment] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)


# handler(args: ArgsModel, ctx: ToolContext) -> str
ToolHandler = Callable[..., Awaitable[str]]


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    args: type[BaseModel]
    handler: ToolHandler
    writes: bool = False
    risk: Risk = Risk.NONE
    enabled: bool = True

    def schema(self) -> dict[str, Any]:
        parameters: dict[str, Any] = self.args.model_json_schema()
        parameters.pop("title", None)
        # OpenAI требует additionalProperties:false-стиля object-схемы; пустые модели нормализуем
        parameters.setdefault("type", "object")
        parameters.setdefault("properties", {})
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        description: str,
        args: type[BaseModel],
        *,
        writes: bool = False,
        risk: Risk = Risk.NONE,
    ) -> Callable[[ToolHandler], ToolHandler]:
        if not _NAME_RE.fullmatch(name):
            raise ValueError(f"имя инструмента {name!r} не подходит OpenAI (нужен snake_case)")

        def decorate(fn: ToolHandler) -> ToolHandler:
            if name in self._tools:
                raise ValueError(f"инструмент {name!r} уже зарегистрирован")
            self._tools[name] = ToolSpec(
                name=name,
                description=description,
                args=args,
                handler=fn,
                writes=writes,
                risk=risk,
            )
            return fn

        return decorate

    # --- чтение ---

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise UnknownTool(name) from exc

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self, *, include_disabled: bool = False) -> list[str]:
        return [t.name for t in self._tools.values() if include_disabled or t.enabled]

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values() if t.enabled]

    # --- управление (dev/эксперименты, kill switch по инструментам) ---

    def set_enabled(self, name: str, enabled: bool) -> None:
        self.get(name).enabled = enabled

    def clear(self) -> None:
        self._tools.clear()


registry = ToolRegistry()
