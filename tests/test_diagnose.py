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


#: Ключ z.ai/bigmodel настоящего образца: <32 hex>.<16 алфавитно-цифровых>.
ZAI_KEY = "0f1e2d3c4b5a69788796a5b4c3d2e1f0.SECRETtail000001"


def test_auth_hint_names_the_mismatch() -> None:
    hint = auth_hint_for(base_url="https://zenmux.ai/api/v1/", api_key=ZAI_KEY)
    assert "выдан z.ai" in hint and "zenmux.ai" in hint
    assert "SECRETtail000001" not in hint, "секретная половина ключа наружу не уходит"
    assert "GLM_BASE_URL=https://api.z.ai/api/paas/v4/" in hint
    assert "open.bigmodel.cn" in hint, "у z.ai две зоны с раздельным балансом — надо сказать"


def test_sk_ai_v1_belongs_to_zenmux_not_z_ai() -> None:
    """Префикс sk-ai-v1- выглядит как «AI-ключ z.ai» и годами вводил в заблуждение: это ZenMux."""
    hint = auth_hint_for(base_url="https://api.z.ai/api/paas/v4/", api_key="sk-ai-v1-SECRETVALUE")
    assert "выдан ZenMux" in hint and "api.z.ai" in hint
    assert "SECRETVALUE" not in hint
    assert "GLM_BASE_URL=https://zenmux.ai/api/v1/" in hint


@pytest.mark.parametrize(
    ("base_url", "api_key"),
    [
        # ключ и адрес совпадают: обе зоны z.ai считаются «своими» для этого формата
        ("https://api.z.ai/api/paas/v4/", ZAI_KEY),
        ("https://open.bigmodel.cn/api/paas/v4", ZAI_KEY),
        ("https://api.z.ai/api/coding/paas/v4", ZAI_KEY),
        ("https://zenmux.ai/api/v1/", "sk-ss-v1-abc"),
        ("https://zenmux.ai/api/v1/", "sk-cs-v1-abc"),
        ("https://zenmux.ai/api/v1/", "sk-ai-v1-abc"),
        ("https://openrouter.ai/api/v1/", "sk-or-v1-abc"),
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


# ------------------------------------------------- OpenRouter: кредиты, имена, reasoning


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Error code: 402 - Insufficient credits", "402"),
        ("PaymentRequiredError: This request requires more credits", "402"),
        ("Error code: 400 - 'glm-5.2' is not a valid model ID", "префиксом автора"),
        ("Error code: 400 - Reasoning is mandatory for this endpoint", "LLM_THINKING_PARAM=false"),
        ("Error code: 403 - input flagged by moderation", "модерация"),
        ("NotFoundError: Error code: 404 - Not Found", "имя модели"),
    ],
)
def test_openrouter_specific_remedies(raw: str, expected: str) -> None:
    out = diagnose(raw)
    assert expected in out, out


def test_openrouter_key_is_recognised() -> None:
    hint = auth_hint_for(base_url="https://api.z.ai/api/paas/v4/", api_key="sk-or-v1-SECRET")
    assert "выдан OpenRouter" in hint and "api.z.ai" in hint
    assert "SECRET" not in hint
    assert auth_hint_for(base_url="https://openrouter.ai/api/v1/", api_key="sk-or-v1-abc") == ""


# ------------------------------------------- z.ai/bigmodel: бизнес-код конкретнее HTTP-статуса


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Error code: 429 - {'code': '1113'}", "пополни счёт"),
        ("Error code: 401 - {'code': '1003'}", "перевыпусти его в консоли"),
        ("Error code: 400 - {'code': '1211', 'message': 'model not exists'}", "сверь MODEL_*"),
        ("Error code: 400 - {'code': '1210', 'message': 'bad params'}", "отверг параметры"),
        ("Error code: 400 - {'code': '1261'}", "промпт длиннее лимита"),
        ("Error code: 429 - {'code': '1304'}", "дневной лимит"),
        ("Error code: 403 - {'code': '1311'}", "не даёт доступа"),
    ],
)
def test_zai_business_codes_are_read_from_the_body(raw: str, expected: str) -> None:
    """«429» не отличает «нет денег» от «слишком быстро» — у z.ai в ответе есть код, читаем его."""
    out = diagnose(raw)
    assert expected in out, out


def test_numeric_codes_match_on_digit_boundaries() -> None:
    """`1000` не должно срабатывать на `max_tokens: 10000`, иначе диагноз врёт."""
    out = diagnose("Error code: 400 - max_tokens: 10000 exceeds limit")
    assert "аутентификация" not in out, out  # «1000» внутри «10000» — не код ошибки
    assert "аутентификация не прошла" in diagnose("Error code: 401 - {'code': '1000'}")
