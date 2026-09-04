"""Диагноз сбоев провайдера: причина доходит до владельца, секреты — нет."""

from __future__ import annotations

import pytest

from aegis.platform.gateway.diagnose import diagnose, redact_secrets


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
