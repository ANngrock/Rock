"""Каталог: цены по имени модели и разбор base_url — то, на чём считается бюджет и ходит сеть."""

from __future__ import annotations

import pytest

from aegis.platform.config import Settings
from aegis.platform.gateway.models import PRICES, resolve_spec


def test_router_names_get_router_prices() -> None:
    cfg = Settings(_env_file=None, _env_prefix="T_", model_brain="z-ai/glm-4.6", glm_api_key="k")
    spec = resolve_spec("brain", cfg)
    assert (spec.name, spec.in_usd_per_m, spec.out_usd_per_m) == ("z-ai/glm-4.6", 0.35, 1.54)
    assert spec.supports_thinking is True, "роль brain умеет thinking независимо от роутера"


def test_unknown_price_falls_back_to_role_price_without_crash() -> None:
    """Незнакомое имя не должно обнулять бюджет: считаем по прайсу роли и предупреждаем."""
    from aegis.platform.gateway.models import CATALOG

    cfg = Settings(_env_file=None, _env_prefix="T_", model_fast="мой-хостed-glm", glm_api_key="k")
    spec = resolve_spec("fast", cfg)
    assert spec.name == "мой-хостed-glm"
    assert (spec.in_usd_per_m, spec.out_usd_per_m) == (
        CATALOG["fast"].in_usd_per_m,
        CATALOG["fast"].out_usd_per_m,
    )


def test_default_role_prices_are_catalog_values() -> None:
    cfg = Settings(_env_file=None, _env_prefix="T_", glm_api_key="k")
    from aegis.platform.gateway.models import CATALOG

    assert resolve_spec("brain", cfg) is CATALOG["brain"], "без переопределения имя не трогаем"


@pytest.mark.parametrize("role", ["brain", "vision", "fast", "embed"])
def test_every_catalog_name_has_a_price(role: str) -> None:
    from aegis.platform.gateway.models import CATALOG

    assert CATALOG[role].name in PRICES, "цена для имени из каталога обязана быть в PRICES"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://zenmux.ai/api/v1/chat/completions", "https://zenmux.ai/api/v1/"),
        ("https://api.z.ai/api/paas/v4/", "https://api.z.ai/api/paas/v4/"),
        ("https://api.z.ai/api/paas/v4", "https://api.z.ai/api/paas/v4/"),
        ("  https://zenmux.ai/api/v1/responses  ", "https://zenmux.ai/api/v1/"),
    ],
)
def test_base_url_is_normalized(raw: str, expected: str) -> None:
    """Открытый URL эндпоинта из чата — обычное дело; клиент дописывает путь сам."""
    cfg = Settings(_env_file=None, _env_prefix="T_", glm_base_url=raw, glm_api_key="k")
    assert cfg.glm_base_url == expected
