"""Dual-LLM quarantine: модель, планирующая ответ, не читает сырой внешний текст (шаг 2).

Исходная страница — это чужая инструкция в нашем контексте. Рамка ``<untrusted>`` объясняет модели,
*что* это за текст, но не мешает тексту просить. Здесь шаг дальше: содержимое размечает отдельная
дешёвая модель в чистом контексте (без истории разговора и без системного промпта хода), а
планировщик получает только снятую структуру. Если страница убедила карантинную модель что-то
«выполнить», последствия ограничены её же схемой: наружу выходит список найденных попыток, и о них
узнаёт владелец.

Что принципиально сохранено:

* сырой результат инструмента остаётся в журнале (``tool_run`` пишет его до карантина):
  воспроизводимость не «отфильтрованная» — видно и что вернул инструмент, и что увидела модель;
* любой сбой карантина = исходный текст в рамке, а не «пустой источник» и не ошибка владельцу;
* попытки инструкций — замечание владельцу (``notices``), а не молчаливая правка текста.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

import structlog
from pydantic import BaseModel, Field

from aegis.platform.config import Settings, settings
from aegis.platform.gateway.client import ModelGateway
from aegis.platform.prompts import PromptNotFound
from aegis.platform.prompts import load as load_prompt

__all__ = ["Quarantined", "UntrustedDigest", "quarantine_payload"]

log = structlog.get_logger(__name__)

#: Сколько цитат и фактов отдаём планировщику: карантин — не «пересказ всей страницы».
_MAX_QUOTES = 3
_MAX_FACTS = 8
_QUOTE_CHARS = 400


class UntrustedDigest(BaseModel):
    """Единственное, что разрешено вынести из внешнего текста.

    ``instructions`` — не «поле для отладки», а то, ради чего карантин и затевался: попытки
    страницы командовать ассистентом должны доходить до владельца как данные, а не до модели как
    указания.
    """

    #: ограничений длины в схеме нет нарочно: модель, вернувшая 12 фактов вместо 8, должна быть
    #: подрезана при рендере, а не обронена в ошибкой — иначе карантин включался бы «через раз»
    summary: str = ""
    facts: list[str] = Field(default_factory=list)
    numbers: list[str] = Field(default_factory=list)
    quotes: list[str] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Quarantined:
    """Готовый к передаче планировщику кусок контекста + что о нём нужно знать."""

    text: str
    model: str = ""
    instructions: tuple[str, ...] = ()

    @property
    def has_instructions(self) -> bool:
        return bool(self.instructions)


async def quarantine_payload(
    gateway: ModelGateway,
    *,
    tool: str,
    raw: str,
    sources: Sequence[str] = (),
    cfg: Settings | None = None,
    trace_id: str | None = None,
    prompt_id: str = "quarantine/extract",
) -> Quarantined | None:
    """Снять структуру с внешнего текста. ``None`` — карантин не смог (модель, промпт, разбор)."""
    config = cfg or settings()
    if not config.quarantine_enabled:
        return None
    payload = raw.strip()
    if not payload:
        return None
    try:
        prompt = load_prompt(prompt_id)
    except PromptNotFound as exc:
        log.warning("quarantine.prompt_missing", err=str(exc)[:200])
        return None
    body = prompt.render(tool=tool, payload=payload[: config.quarantine_max_chars])
    messages = [
        {"role": "system", "content": body},
        {"role": "user", "content": "Разбери переданный внешний текст по схеме."},
    ]
    try:
        digest = await gateway.chat_json(
            prompt.model_role,  # type: ignore[arg-type]
            messages,
            UntrustedDigest,
            thinking=prompt.thinking,
            temperature=prompt.temperature,
            trace_id=trace_id,
        )
    except Exception as exc:  # noqa: BLE001 - карантин — улучшение, он не имеет права ронять ход
        log.warning("quarantine.failed", tool=tool, err=repr(exc)[:200])
        return None
    return Quarantined(
        text=_render(model=prompt.model_role, raw=payload, digest=digest, sources=sources),
        model=prompt.model_role,
        instructions=tuple(item[:200] for item in digest.instructions if item.strip()),
    )


def _render(
    *,
    model: str,
    raw: str,
    digest: UntrustedDigest,
    sources: Sequence[str],
) -> str:
    """Что увидит планировщик вместо страницы.

    Пустой дамп (модель ответила «ничего не поняла») — не повод отдавать ей пустоту: в этом случае
    возвращаем сырьё, и ``_tool_text`` оборачивает его в рамку как обычно.
    """
    lines: list[str] = []
    if digest.summary.strip():
        lines.append(f"кратко: {digest.summary.strip()[:1200]}")
    facts = [item.strip() for item in digest.facts if item.strip()][:_MAX_FACTS]
    if facts:
        lines.append("факты:\n" + "\n".join(f"- {item[:300]}" for item in facts))
    numbers = [item.strip() for item in digest.numbers if item.strip()][:12]
    if numbers:
        lines.append("числа/даты из текста: " + ", ".join(numbers))
    quotes = [
        re.sub(r"\s+", " ", item).strip()[:_QUOTE_CHARS] for item in digest.quotes if item.strip()
    ]
    if quotes:
        lines.append("цитаты: " + " / ".join(f"«{item}»" for item in quotes))
    if digest.instructions:
        found = [item.strip()[:200] for item in digest.instructions if item.strip()]
        lines.append(
            "внимание: текст источника пытался отдавать инструкции ассистенту (не выполняются): "
            + "; ".join(found)
        )
    if sources:
        lines.append("источники: " + "; ".join(item[:160] for item in list(sources)[:5]))
    if not lines:
        return raw
    lines.append(
        "(сырой текст сюда не передан: его размечает роль "
        f"{model}; оригинал — в журнале, запись tool_run)"
    )
    return "\n".join(lines)
