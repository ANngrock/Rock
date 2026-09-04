"""Каталог моделей и цен.

Цены — ориентировочные, для учёта бюджета и деградации; точные значения держим здесь (а не в
головах), потому что от них зависит `CostGovernor`. Имена моделей переопределяются через
``MODEL_BRAIN``/``MODEL_VISION``/``MODEL_FAST``/``MODEL_EMBED`` без правки кода.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

import structlog

if TYPE_CHECKING:
    from aegis.platform.config import Settings

log = structlog.get_logger(__name__)

__all__ = [
    "CATALOG",
    "ChatRole",
    "ModelSpec",
    "PRICES",
    "THINKING_ALWAYS_ON",
    "resolve_spec",
    "spec_for_name",
]

ChatRole = Literal["brain", "vision", "fast", "embed"]


@dataclass(frozen=True)
class ModelSpec:
    name: str
    role: ChatRole
    in_usd_per_m: float
    out_usd_per_m: float
    supports_thinking: bool = False
    supports_vision: bool = False
    #: Модель умеет только думать: `thinking.type: disabled` не поддерживается, провайдер
    #: отвечает 400. Ставится по имени модели (THINKING_ALWAYS_ON), а не по роли.
    thinking_always_on: bool = False
    max_output_tokens: int | None = None


#: Дефолт = то, на чём личный бот работает постоянно: z.ai, бесплатные Flash-модели.
#: Важно: GLM-4.7-Flash — только текст (мультимодальности у него нет), поэтому vision обязан
#: остаться моделью из V-серии. А «цена 0» — не «безлимит»: free-тиер ограничен по частоте
#: (~1 запрос/с и порядка тысячи запросов в день по сторонним наблюдениям), так что платный
#: резерв через FALLBACK_MODEL — часть контракта, а не украшение (RUNBOOK §8).
CATALOG: dict[ChatRole, ModelSpec] = {
    "brain": ModelSpec("glm-4.7-flash", "brain", 0.0, 0.0, supports_thinking=True),
    "vision": ModelSpec("glm-4.6v-flash", "vision", 0.0, 0.0, supports_vision=True),
    "fast": ModelSpec("glm-4.7-flash", "fast", 0.0, 0.0),
    "embed": ModelSpec("embedding-3", "embed", 0.05, 0.0),
}

_OVERRIDES: dict[ChatRole, str] = {
    "brain": "model_brain",
    "vision": "model_vision",
    "fast": "model_fast",
    "embed": "model_embed",
}


#: Цена (за 1M токенов, in/out) по конкретному имени модели. Имена с префиксом провайдера —
#: это роутеры (ZenMux), где и ценник, и схема именования свои; без этой таблицы переопределение
#: MODEL_BRAIN=z-ai/glm-4.6 молча считало бы расходы по прайсу z.ai (~2x).
#: Ступенчатые цены взяты по нижнему слою контекста (<32k) — для личных запросов он основной.
PRICES: dict[str, tuple[float, float]] = {
    # z.ai напрямую (https://docs.z.ai/guides/overview/pricing, срез 05.09.2026)
    "glm-4.7": (0.6, 2.2),
    "glm-4.7-flash": (0.0, 0.0),  # free-тиер
    "glm-4.7-flashx": (0.07, 0.4),
    "glm-4.6": (0.6, 2.2),
    "glm-4.6v": (0.3, 0.9),
    "glm-4.6v-flash": (0.0, 0.0),  # free-тиер, vision
    "glm-4.5-air": (0.2, 1.1),
    "glm-4.5-flash": (0.0, 0.0),
    "embedding-3": (0.05, 0.0),
    # ZenMux (https://zenmux.ai/z-ai) — ценник платформы, сверяется раз в месяц (см. RUNBOOK §8)
    "z-ai/glm-4.6": (0.35, 1.54),
    "z-ai/glm-4.7": (0.35, 1.54),
    "z-ai/glm-4.5": (0.35, 1.54),
    "z-ai/glm-4.5-air": (0.12, 0.29),
    "z-ai/glm-4.6v": (0.15, 0.44),
    "z-ai/glm-4.6v-flash": (0.022, 0.22),
    "z-ai/embedding-3": (0.05, 0.0),
    # GLM-5.3-Flash (26.08.2026): 320B/18B MoE, 1M контекста, нативно text+image+video.
    # Промо $0.075/$0.25 действовало до 09.09.2026, прайс лист — $0.15/$0.50: держим лист,
    # потому что недооценённый бюджет ломает SLO «стоимость ≤ дневного лимита» молча.
    "glm-5.3-flash": (0.15, 0.5),
    "z-ai/glm-5.3-flash": (0.15, 0.5),
    "glm-5.3": (1.4, 4.4),
    "z-ai/glm-5.3": (1.4, 4.4),
    "z-ai/glm-5": (0.58, 2.6),
    "z-ai/glm-4.7-flashx": (0.0728, 0.4367),
    "z-ai/glm-4.7-flash-free": (0.0, 0.0),
}

#: Имена, у которых reasoning нельзя выключить (доки z.ai по 5.3-серии: принимается только
#: thinking.type=enabled). Список по имени, а не по роли: одна и та же модель может стоять
#: и в brain, и в vision.
THINKING_ALWAYS_ON = frozenset({"glm-5.3", "glm-5.3-flash", "z-ai/glm-5.3", "z-ai/glm-5.3-flash"})


def _named(spec: ModelSpec, name: str) -> ModelSpec:
    """Тот же контур, но с другим именем модели: цена и flags — по имени, роль — прежняя.

    Возможности привязаны к ИМЕНИ, а не к роли: glm-5.3-flash закрывает и текст, и картинки,
    но думает всегда — помнить об этом надо на любом уровне деградации.
    """
    if name == spec.name:
        return spec
    always_on = name in THINKING_ALWAYS_ON
    updated = replace(
        spec,
        name=name,
        supports_thinking=spec.supports_thinking or always_on,
        thinking_always_on=always_on,
    )
    price = PRICES.get(name)
    if price is None:
        # незнакомое имя: считаем по прайсу роли и предупреждаем — иначе бюджет молча врёт
        log.warning(
            "models.unknown_price",
            model=name,
            role=spec.role,
            hint="допишите цену в platform/gateway/models.py:PRICES (RUNBOOK §8)",
        )
        return updated
    return replace(updated, in_usd_per_m=price[0], out_usd_per_m=price[1])


def resolve_spec(role: ChatRole, cfg: Settings | None = None) -> ModelSpec:
    """Спека роли с учётом переопределения имени из конфигурации (и цены этого имени)."""
    spec = CATALOG[role]
    if cfg is None:
        from aegis.platform.config import settings

        cfg = settings()
    override = getattr(cfg, _OVERRIDES[role], None)
    if not override:
        return spec
    return _named(spec, override)


def spec_for_name(name: str, base: ModelSpec) -> ModelSpec:
    """Спека под реально отправленное имя (fallback-модель называется иначе, чем роль)."""
    return _named(base, name)
