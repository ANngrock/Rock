"""Диагноз сбоя обращения к провайдеру — одна строка, которую можно показать владельцу.

Принцип: чтобы понять «401 это или нет сети», человек не должен читать логи контейнера. Но и
вытаскивать текст ошибки провайдера наружу целиком нельзя: там бывают URL, заголовки и случайные
куски запроса. Поэтому наружу уходит только классифицированная, замаскированная строка.
"""

from __future__ import annotations

import re

__all__ = ["diagnose", "redact_secrets"]

# Секреты в текст ошибок попадают через логи библиотек; ни один из паттернов не должен
# пройти в сообщение Telegram.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)\S+"), r"\1<secret>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{4,}"), "Bearer <secret>"),
    (
        re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret)(['\"]?\s*[:=]\s*)\S+"),
        r"\1\2<secret>",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "<secret>"),
)

_MAX_LEN = 220


def redact_secrets(text: str) -> str:
    out = text[:_MAX_LEN]
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out

    # Порядок важен: от специфичного к общему. Ключ — подстрока в repr исключения:
    # openai-ошибки приходят как 'AuthenticationError: Error code: 401 - {...}'


_RULES: tuple[tuple[str, str], ...] = (
    (
        "authenticationerror",
        "401 — ключ не принят. Обнови GLM_API_KEY в .env и пересоздай контейнер",
    ),
    ("401", "401 — ключ не принят. Обнови GLM_API_KEY в .env и пересоздай контейнер"),
    (
        "permissiondeniederror",
        "403 — ключ принят, но доступа к модели нет: проверь тариф и имя модели",
    ),
    (
        "notfounderror",
        "404 — неверный путь: GLM_BASE_URL обязан вести на .../api/paas/v4/",
    ),
    ("404", "404 — неверный путь запроса: сверь GLM_BASE_URL (.../api/paas/v4/)"),
    (
        "ratelimiterror",
        "429 — сработал лимит частоты/квоты: подожди минуту или переведи роль на flash-модель",
    ),
    ("429", "429 — сработал лимит частоты/квоты: подожди минуту"),
    ("content_filter", "провайдер отклонил запрос по файлу содержимого (модерация z.ai)"),
    ("insufficient_balance", "на балансе key'а нет средств — пополни аккаунт z.ai"),
    ("insufficient_quota", "квота ключа исчерпана"),
    (
        "model not found",
        "провайдер не знает такую модель — сверь MODEL_BRAIN/MODEL_FAST/MODEL_VISION с его списком",
    ),
    ("invalid model", "провайдер не знает такую модель — сверь MODEL_* с его списком"),
    ("1210", "провайдер не знает такую модель — сверь MODEL_* с его списком"),
    ("badrequesterror", "400 — провайдер отклонил состав запроса (сообщения/TOOLS/параметры)"),
    (
        "unprocessableentityerror",
        "422 — провайдер не смог обработать запрос (часто: слишком длинный контекст)",
    ),
    ("internalservererror", "5xx на стороне провайдера — обычно проходит само, попробуй позже"),
    ("serviceunavailableerror", "503 — провайдер перегружен, попробуй позже"),
    ("apitimeouterror", "таймаут: провайдер не ответил за {timeout_s:.0f} c — проверь VPN/прокси"),
    ("timeout", "таймаут: провайдер не ответил за {timeout_s:.0f} c"),
    (
        "apiconnectionerror",
        "соединение с провайдером не установлено: DNS, файрвол, VPN или перехват TLS",
    ),
    (
        "certificate verify failed",
        "TLS: сертификат api.z.ai не проходит проверку (перехват/прокси)",
    ),
    ("getaddrinfo", "DNS не разрешает имя хоста провайдера"),
    ("connection refused", "в соединении отказано: порт/прокси блокируют запрос"),
    (
        "connect call failed",
        "порт провайдера не отвечает: исходящие блокирует файрвол/прокси (или DNS врёт)",
    ),
    ("network is unreachable", "сеть недоступна из контейнера"),
    ("apistatuserror", "провайдер ответил ошибкой HTTP"),
)


def diagnose(error: str, *, timeout_s: float = 60.0) -> str:
    """Одна короткая строка с причиной и первым действием.

    Пустой вход — частый случай, когда исключение прилетело не от OpenAI-совместимого
    клиента: тогда честно говорим, что классифицировать нечего.
    """
    clean = redact_secrets(error or "").strip()
    if not clean:
        return "провайдер не дал деталей; смотри `aegis doctor`"
    low = clean.lower()
    for needle, remedy in _RULES:
        if needle in low:
            return remedy.format(timeout_s=timeout_s)
    # классифицировать нечем, но текст может быть полезен: отдаём замаскированный кусок
    return f"ошибка провайдера: {clean[:120]}"
