"""Детерминированные ответы до обращения к модели.

Принцип плана (часть I, п.5): «деградация без LLM — команды и детерминированные парсеры работают
всегда». Для владельца это означает: вопрос с точным ответом в публичном источнике не должен
зависеть ни от дневного бюджета, ни от того, жив ли SearXNG и не лёг ли провайдер моделей. Отсюда
два правила этого модуля:

* перехватываем только то, что реально умеем ответить числом (курсы валют) — всё остальное уходит
  модели как уходило;
* если deterministic-путь не смог (источники молчат), он не выдаёт «уточните запрос»: он оставляет
  владелцу причину и передаёт ход поиску/модели.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from aegis.platform.config import Settings, settings
from aegis.web.rates import RateAnswer, fetch_rates, parse_rate_question

__all__ = ["IntentAnswer", "try_answer"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class IntentAnswer:
    """Ответ, собранный без LLM: текст + что именно сработало + чем подтверждаем."""

    intent: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


def _render(answer: RateAnswer, cfg: Settings) -> str:
    """Один вид у ответа из intent-пути и из инструмента: расхождение тут заметят только цифры."""
    return answer.render(home=cfg.rate_home_currency)


async def try_answer(
    text: str,
    *,
    services: Any = None,
    kv: Any = None,
    cfg: Settings | None = None,
    notices: list[str] | None = None,
) -> IntentAnswer | None:
    """Попытаться ответить детерминированно. None — значит «это не курс, отвечайте обычно».

    ``notices`` — канал, куда кладется причина отказа источников: она обязана дойти до владельца
    вместе с ответом модели, иначе отказ снова превратится в вежливое «поищи точнее».
    """
    cfg = cfg or settings()
    question = parse_rate_question(text, home=cfg.rate_home_currency)
    if question is None:
        return None
    cache = kv if cfg.rate_cache_ttl_s > 0 else None
    answer = await fetch_rates(question, cfg=cfg, kv=cache)
    if answer.verdict == "unavailable":
        causes = "; ".join(answer.causes[:3]) or "источники не ответили"
        log.warning("intent.rates_unavailable", causes=causes[:300])
        if notices is not None:
            notices.append(f"прямые источники курсов не ответили: {causes}")
        return None
    return IntentAnswer(
        intent="rates",
        text=_render(answer, cfg),
        meta={"question": question.raw, **answer.as_meta()},
    )
