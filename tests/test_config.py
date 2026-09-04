"""Конфигурация: пустые значения в .env не ломают старт, секреты не светятся, TZ валидна."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from aegis.platform.config import ConfigError, Settings, override_settings, settings


def load(**kw: object) -> Settings:
    """Настройки «как из воздуха»: ни .env, ни переменные окружения не участвуют."""
    return Settings(_env_file=None, _env_prefix="AEGIS_TEST_ONLY_", **kw)


def test_defaults_are_sane_without_env() -> None:
    cfg = load()
    assert cfg.timezone == "Europe/Moscow"
    assert cfg.base_currency == "RUB"
    assert cfg.daily_budget_usd == 2.0
    assert cfg.max_iterations == 8
    assert cfg.pending_ttl_seconds == 3600


def test_empty_env_values_are_treated_as_unset() -> None:
    cfg = load(telegram_owner_id="", glm_base_url="", telegram_alerts_chat_id="")
    assert cfg.telegram_owner_id is None
    assert cfg.telegram_alerts_chat_id is None
    assert cfg.glm_base_url == "https://api.z.ai/api/paas/v4/"


def test_missing_runtime_keys_are_listed() -> None:
    cfg = load(telegram_bot_token=None, telegram_owner_id=None, glm_api_key=None)
    assert cfg.missing_runtime_keys() == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "GLM_API_KEY"]
    with pytest.raises(ConfigError):
        cfg.require_runtime()


def test_require_runtime_returns_self_when_complete() -> None:
    cfg = load(
        telegram_bot_token="123:abc",
        telegram_owner_id=42,
        glm_api_key="k",
    )
    assert cfg.require_runtime() is cfg
    assert cfg.missing_runtime_keys() == []


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        load(timezone="Mars/Olympus")


def test_currency_is_normalized() -> None:
    assert load(base_currency="rub").base_currency == "RUB"
    with pytest.raises(ValidationError):
        load(base_currency="рубли")


def test_secrets_are_not_printed_by_redacted() -> None:
    cfg = load(glm_api_key="super-secret-token", telegram_bot_token="bot:secret")
    dumped = cfg.redacted()
    assert "super-secret-token" not in str(dumped)
    assert "bot:secret" not in str(dumped)
    assert dumped["glm_api_key"] == "***"


def test_secretstr_rejects_plain_repr_in_logs() -> None:
    cfg = load(glm_api_key="secret")
    assert isinstance(cfg.glm_api_key, SecretStr)
    assert "secret" not in repr(cfg.glm_api_key)


def test_override_settings_is_scoped() -> None:
    before = settings().daily_budget_usd
    with override_settings(daily_budget_usd=0.25):
        assert settings().daily_budget_usd == 0.25
    assert settings().daily_budget_usd == before


def test_is_production_flag() -> None:
    assert load(env="production").is_production is True
    assert load(env="dev").is_production is False
