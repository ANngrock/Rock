"""Каталог моделей и цен.

Цены — ориентировочные, для учёта бюджета и деградации; точные значения держим здесь (а не в
головах), потому что от них зависит `CostGovernor`. Имена моделей переопределяются через
``MODEL_BRAIN``/``MODEL_VISION``/``MODEL_FAST``/``MODEL_EMBED`` без правки кода.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from aegis.platform.config import Settings

__all__ = ["CATALOG", "ChatRole", "ModelSpec", "resolve_spec"]

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


def resolve_spec(role: ChatRole, cfg: Settings | None = None) -> ModelSpec:
    """Спека роли с учётом переопределения имени из конфигурации."""
    spec = CATALOG[role]
    if cfg is None:
        from aegis.platform.config import settings

        cfg = settings()
    override = getattr(cfg, _OVERRIDES[role], None)
    if not override or override == spec.name:
        return spec
    return replace(spec, name=override)
