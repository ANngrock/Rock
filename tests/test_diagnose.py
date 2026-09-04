"""Диагноз сбоев провайдера: причина доходит до владельца, секреты — нет."""

from __future__ import annotations

import pytest

from aegis.platform.gateway.diagnose import (
    auth_hint_for,
    diagnose,
    gateway_auth_hint,
    redact_secrets,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AuthenticationError: Error code: 401 - {'error': 'invalid api key'}", "401"),
        ("PermissionDeniedError: Error code: 403", "403"),
        ("NotFoundError: Error code: 404 - Not Found", "404"),
        ("RateLimitError: Error code: 429 - too many requests", "429"),
        ("InternalServerError: 503 server overloaded", "5xx"),
        ("APITimeoutError: Request timed out.", "таймаут"),
        ("APIConnectionError: Connection error.", "соединение"),
        (
            "ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] "
            "certificate verify failed",
            "TLS",
        ),
        ("OSError: [Errno 111] Connect call failed ('127.0.0.1', 443)", "порт провайдера"),
        ("Error code: 400 - {'error': {'code': 'content_filter'}}", "модерация"),
        ("insufficient_balance detail", "баланс"),
    ],
)
def test_known_causes_map_to_actions(raw: str, expected: str) -> None:
    assert expected in diagnose(raw, timeout_s=45)


def test_timeout_uses_configured_value() -> None:
    assert "за 45 c" in diagnose("APITimeoutError: timed out", timeout_s=45)


def test_unknown_error_is_forwarded_masked_and_short() -> None:
    out = diagnose("WeirdUpstreamError: " + "x" * 500)
    assert out.startswith("ошибка провайдера:")
    assert len(out) <= 160


def test_empty_error_still_explains_next_step() -> None:
    assert "doctor" in diagnose("")


def test_secrets_never_survive() -> None:
    raw = (
        "APIResponse headers: {'authorization': 'Bearer sk-proj-LEAKME123456', "
        "'x-api-key': 'LEAKME-2'} api_key=LEAKME3 query?api_key=LEAKME4"
    )
    out = diagnose(raw)
    for leak in ("LEAKME123456", "LEAKME-2", "LEAKME3", "LEAKME4", "sk-proj-"):
        assert leak not in out, f"секрет утёк в сообщение: {leak}"
    assert "<secret>" in out


def test_redact_is_idempotent_on_clean_text() -> None:
    clean = "NotFoundError: Error code: 404"
    assert redact_secrets(redact_secrets(clean)) == clean


# ------------------------------- ключ и endpoint: один сервис или нет — видно сразу


def test_auth_hint_names_the_mismatch() -> None:
    hint = auth_hint_for(base_url="https://zenmux.ai/api/v1/", api_key="sk-ai-v1-SECRETVALUE")
    assert "выдан z.ai" in hint and "zenmux.ai" in hint
    assert "SECRETVALUE" not in hint, "префикс — не секрет, сам ключ наружу не уходит"
    assert "GLM_BASE_URL=https://api.z.ai/api/paas/v4/" in hint


@pytest.mark.parametrize(
    ("base_url", "api_key"),
    [
        ("https://api.z.ai/api/paas/v4/", "sk-ai-v1-abc"),  # ключ и адрес совпадают
        ("https://zenmux.ai/api/v1/", "sk-ss-v1-abc"),
        ("https://zenmux.ai/api/v1/", "sk-cs-v1-abc"),
        ("https://api.z.ai/api/paas/v4/", "zzz-неизвестный-ключ"),  # не угадываем
        ("", "sk-ai-v1-abc"),
        ("https://zenmux.ai/api/v1/", ""),
    ],
)
def test_auth_hint_stays_silent_when_it_cannot_be_sure(base_url: str, api_key: str) -> None:
    assert auth_hint_for(base_url=base_url, api_key=api_key) == ""


def test_context_is_appended_to_the_remedy() -> None:
    out = diagnose("Error code: 401", context="проверь TLS")
    assert out.endswith("· проверь TLS")


def test_gateway_without_auth_hint_method_is_fine() -> None:
    assert gateway_auth_hint(object()) == ""
