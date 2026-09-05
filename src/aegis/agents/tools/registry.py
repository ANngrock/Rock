"""Реестр инструментов: Pydantic-модель аргументов → OpenAI function schema.

Инструмент = описание + схема + handler + метаданные риска. Метаданные (``writes``, ``risk``)
существуют ради policy engine: регистрация инструмента уже говорит, надо ли его подтверждать.

Результат — :class:`ToolResult`, а не голая строка: у содержимого есть степень доверия, и без неё
невозможно ни честное воспроизведение хода (M1: чем именно подтверждали ответ), ни карантин
недоверенного контента (M2: что нельзя считать инструкцией). Handler'ы, которым маркировать нечего,
по-прежнему возвращают ``str`` — supervisor понимает оба варианта, иначе ``trust`` пришлось бы
выставлять руками в десятке мест и обязательно где-то забыть.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from aegis.governance.policy import Risk, Trust

__all__ = [
    "Attachment",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "TrustLevel",
    "UnknownTool",
    "registry",
]

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


#: Откуда данные: ``owner`` — написал владелец, ``system`` — наши вычисления и БД, ``untrusted`` —
#: внешний мир (веб, файл, картинка). Это не метка вежливости, а граница безопасности: untrusted
#: не имеет права превращаться в инструкцию (принцип 2 системного промпта).
TrustLevel = Literal["owner", "system", "untrusted"]


@dataclass(slots=True)
class ToolResult:
    """Что инструмент ответил и насколько этому можно верить.

    ``ref_id`` — ссылка на исходник внутри хода: по ней отдаётся сырой фрагмент владельцу
    (``show_source`` в M2), не протаскивая его в контекст планировщика.
    """

    content: str
    trust: TrustLevel = "system"
    source: str = ""
    media_type: str = "text/plain"
    ref_id: str | None = None

    @property
    def is_untrusted(self) -> bool:
        return self.trust == "untrusted"

    @classmethod
    def untrusted(cls, content: str, source: str = "", *, ref_id: str | None = None) -> ToolResult:
        return cls(content=content, trust="untrusted", source=source, ref_id=ref_id)

    @classmethod
    def coerce(cls, value: str | ToolResult) -> ToolResult:
        """Принять и строку, и размеченный результат: реестр не диктует handler'ам лишний слой."""
        return value if isinstance(value, ToolResult) else cls(content=str(value))

    def as_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "trust": self.trust,
            "source": self.source,
            "media_type": self.media_type,
            "ref_id": self.ref_id,
        }


# handler(args: ArgsModel, ctx: ToolContext) -> str | ToolResult
ToolHandler = Callable[..., Awaitable[str | ToolResult]]


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

    def schema_sha(self) -> bytes:
        """Хэш открытой модели инструментов — 32 байта, доказывающие, *чем именно* был вооружён
        агент в том ходе (M1: «модель та же» ≠ «возможности те же»).

        Считаем от отсортированного по имени списка: ``schemas()`` идёт в порядке регистрации, а
        перестановка инструментов местами не должна выглядеть как изменение поведения.
        """
        from aegis.platform.canonical import canonical_sha256

        ordered = sorted(self.schemas(), key=lambda item: str(item.get("function", {}).get("name")))
        return canonical_sha256(ordered)

    # --- управление (dev/эксперименты, kill switch по инструментам) ---

    def set_enabled(self, name: str, enabled: bool) -> None:
        self.get(name).enabled = enabled

    def clear(self) -> None:
        self._tools.clear()


registry = ToolRegistry()
