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

__all__ = ["CATALOG", "ChatRole", "ModelSpec", "PRICES", "resolve_spec"]

ChatRole = Literal["brain", "vision", "fast", "embed"]


@dataclass(frozen=True)
class ModelSpec:
    name: str
    role: ChatRole
    in_usd_per_m: float
    out_usd_per_m: float
    supports_thinking: bool = False
    supports_vision: bool = False
    max_output_tokens: int | None = None


CATALOG: dict[ChatRole, ModelSpec] = {
    "brain": ModelSpec("glm-4.6", "brain", 0.6, 2.2, supports_thinking=True),
    "vision": ModelSpec("glm-4.6v", "vision", 0.6, 1.8, supports_vision=True),
    "fast": ModelSpec("glm-4.5-air", "fast", 0.2, 1.1),
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
    # z.ai напрямую
    "glm-4.6": (0.6, 2.2),
    "glm-4.6v": (0.6, 1.8),
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
    "z-ai/glm-4.6v-flash-free": (0.0, 0.0),
    "z-ai/embedding-3": (0.05, 0.0),
}


def resolve_spec(role: ChatRole, cfg: Settings | None = None) -> ModelSpec:
    """Спека роли с учётом переопределения имени из конфигурации (и цены этого имени)."""
    spec = CATALOG[role]
    if cfg is None:
        from aegis.platform.config import settings

        cfg = settings()
    override = getattr(cfg, _OVERRIDES[role], None)
    if not override or override == spec.name:
        return spec
    price = PRICES.get(override)
    if price is None:
        # незнакомое имя: считаем по прайсу роли и предупреждаем — иначе бюджет молча врёт
        log.warning(
            "models.unknown_price",
            model=override,
            role=role,
            hint="допишите цену в platform/gateway/models.py:PRICES (RUNBOOK §8)",
        )
        return replace(spec, name=override)
    return replace(spec, name=override, in_usd_per_m=price[0], out_usd_per_m=price[1])
