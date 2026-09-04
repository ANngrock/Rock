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

    cfg = Settings(_env_file=None, _env_prefix="T_", model_embed="мой-хостed-glm", glm_api_key="k")
    spec = resolve_spec("embed", cfg)
    assert spec.name == "мой-хостed-glm"
    assert (spec.in_usd_per_m, spec.out_usd_per_m) == (
        CATALOG["embed"].in_usd_per_m,
        CATALOG["embed"].out_usd_per_m,
    )
    assert spec.in_usd_per_m > 0, "иначе проверка «не обнуляем бюджет» ничего не проверяет"


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


def test_glm_5_3_flash_price_and_flags_by_name() -> None:
    cfg = Settings(
        _env_file=None,
        _env_prefix="T_",
        model_brain="z-ai/glm-5.3-flash",
        model_vision="z-ai/glm-5.3-flash",
        glm_api_key="k",
    )
    brain = resolve_spec("brain", cfg)
    assert (brain.in_usd_per_m, brain.out_usd_per_m) == (0.15, 0.5)
    assert brain.supports_thinking is True
    assert brain.thinking_always_on is True, "у 5.3-серии reasoning не выключается"
    vision = resolve_spec("vision", cfg)
    assert vision.supports_vision is True, "та же модель нативно мультимодальная"
    assert vision.thinking_always_on is True


def test_model_without_price_entry_keeps_role_price() -> None:
    cfg = Settings(_env_file=None, _env_prefix="T_", model_brain="glm-4.6", glm_api_key="k")
    spec = resolve_spec("brain", cfg)
    assert spec.thinking_always_on is False, "glm-4.6 умеет думать по запросу, а не всегда"


def test_every_price_key_is_a_real_model_name() -> None:
    """Ключ PRICES — имя для провайдера: опечатка здесь = «model not found» в бою."""
    from aegis.platform.gateway.models import CATALOG

    for role, spec in CATALOG.items():
        assert spec.name in PRICES, f"{role}: у имени каталога нет цены"
        assert " " not in spec.name and spec.name == spec.name.strip()


# ------------------------------------------------------------------ z.ai, GLM-4.7-Flash


def test_free_flash_is_priced_zero_but_still_thinks_on_demand() -> None:
    """GLM-4.7-Flash бесплатен, и reasoning у него ВЫКЛЮЧАЕТСЯ (в отличие от 5.3-серии).

    Это не мелочь: уровень деградации бюджета «выключить thinking» снова работает, и роль fast
    остаётся экономной, а не «вечно думающей».
    """
    cfg = Settings(_env_file=None, _env_prefix="T_", model_brain="glm-4.7-flash", glm_api_key="k")
    spec = resolve_spec("brain", cfg)
    assert (spec.in_usd_per_m, spec.out_usd_per_m) == (0.0, 0.0)
    assert spec.supports_thinking is True
    assert spec.thinking_always_on is False, "4.7-серия принимает thinking.type=disabled"


def test_paid_flashx_price_is_used_for_the_fallback_name() -> None:
    """Резерв может быть платным, пока основная модель бесплатна: цена — по фактическому имени."""
    from aegis.platform.gateway.models import CATALOG, spec_for_name

    base = CATALOG["brain"]
    assert base.in_usd_per_m == 0.0
    spec = spec_for_name("glm-4.7-flashx", base)
    assert spec.role == "brain"
    assert (spec.in_usd_per_m, spec.out_usd_per_m) == (0.07, 0.4)


def test_vision_role_stays_on_a_vision_model() -> None:
    """У GLM-4.7-Flash нет мультимодальности: роль vision обязана быть V-серией."""
    from aegis.platform.gateway.models import CATALOG

    assert CATALOG["brain"].supports_vision is False
    assert "v" in CATALOG["vision"].name, CATALOG["vision"].name
    assert CATALOG["vision"].supports_vision is True
