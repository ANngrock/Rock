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
    "host_of",
    "price_for",
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


#: Цена (за 1M токенов, in/out). Ключ — имя модели; если у разных провайдеров одно и то же имя
#: стоит по-разному, ключ — строка `хост/имя`, и она выигрывает у голого имени: `z-ai/glm-4.6v`
#: на OpenRouter $0.30/$0.90, на ZenMux $0.15/$0.44. Без этой таблицы переопределение
#: MODEL_BRAIN=z-ai/glm-4.6 молча считало бы расходы по прайсу z.ai (~2x).
#: Ступенчатые цены взяты по нижнему слою контекста — для личных запросов он основной.
#: Промо и скидки внутри провайдера не учитываем: держим лист, потому что недооценённый бюджет
#: ломает SLO «стоимость ≤ дневного лимита» молча, а переоценённый лишь раньше посадит thinking.
PRICES: dict[str, tuple[float, float]] = {
    # OpenRouter (страницы моделей openrouter.ai/<id>, срез 05.09.2026; +5.5 % на пополнение)
    "openrouter.ai/z-ai/glm-5.2": (1.4, 4.4),  # у части хостов сейчас скидка 70 %
    "openrouter.ai/z-ai/glm-4.7-flash": (0.06, 0.4),
    "openrouter.ai/z-ai/glm-4.6v": (0.3, 0.9),
    # z.ai напрямую (https://docs.z.ai/guides/overview/pricing, срез 05.09.2026; у
    # open.bigmodel.cn те же модели, но биллинг в юанях и отдельный баланс)
    "glm-5.2": (1.4, 4.4),
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
    "z-ai/glm-4.6v": (0.15, 0.44),  # ZenMux; на OpenRouter то же имя дороже (scoped-строка выше)
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


def host_of(url: str | None) -> str:
    """Хост из base_url — он же ключ скоупинга цен в PRICES."""
    return (url or "").split("//")[-1].split("/")[0].casefold()


def price_for(name: str, host: str = "") -> tuple[float, float] | None:
    """Цена имени у конкретного хоста: `хост/имя` важнее голого имени."""
    if host:
        scoped = PRICES.get(f"{host}/{name}")
        if scoped is not None:
            return scoped
    return PRICES.get(name)


def _named(spec: ModelSpec, name: str, *, host: str = "") -> ModelSpec:
    """Тот же контур, но с другим именем (и у другого хоста): цена — по имени, роль — прежняя.

    Возможности привязаны к ИМЕНИ, а не к роли: glm-5.3-flash закрывает и текст, и картинки,
    но думает всегда. Хост тут потому, что у роутеров одно и то же имя стоит других денег:
    дефолт каталога, не сменив имени, на чужом хосте обязан дорожать — иначе бюджет врёт.
    """
    price = price_for(name, host)
    always_on = name in THINKING_ALWAYS_ON
    thinking = spec.supports_thinking or always_on
    same_price = price is None or price == (spec.in_usd_per_m, spec.out_usd_per_m)
    if (
        name == spec.name
        and same_price
        and always_on == spec.thinking_always_on
        and thinking == spec.supports_thinking
    ):
        return spec
    updated = replace(
        spec,
        name=name,
        supports_thinking=thinking,
        thinking_always_on=always_on,
    )
    if price is None:
        # незнакомое имя: считаем по прайсу роли и предупреждаем — иначе бюджет молча врёт
        log.warning(
            "models.unknown_price",
            model=name,
            role=spec.role,
            host=host,
            hint="допишите цену в platform/gateway/models.py:PRICES (RUNBOOK §8)",
        )
        return updated
    return replace(updated, in_usd_per_m=price[0], out_usd_per_m=price[1])


def resolve_spec(role: ChatRole, cfg: Settings | None = None) -> ModelSpec:
    """Спека роли с учётом имени из конфигурации — и цены этого имени у этого провайдера."""
    spec = CATALOG[role]
    if cfg is None:
        from aegis.platform.config import settings

        cfg = settings()
    if role == "embed":
        host = host_of(cfg.embed_base_url or cfg.glm_base_url)
    else:
        host = host_of(cfg.glm_base_url)
    override = getattr(cfg, _OVERRIDES[role], None)
    return _named(spec, override or spec.name, host=host)


def spec_for_name(name: str, base: ModelSpec, *, host: str = "") -> ModelSpec:
    """Спека под реально отправленное имя (fallback-модель называется иначе, чем роль)."""
    return _named(base, name, host=host)
