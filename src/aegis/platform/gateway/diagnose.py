"""Диагноз сбоя обращения к провайдеру — одна строка, которую можно показать владельцу.

Принцип: чтобы понять «401 это или нет сети», человек не должен читать логи контейнера. Но и
вытаскивать текст ошибки провайдера наружу целиком нельзя: там бывают URL, заголовки и случайные
куски запроса. Поэтому наружу уходит только классифицированная, замаскированная строка.
"""

from __future__ import annotations

import re

__all__ = ["auth_hint_for", "diagnose", "gateway_auth_hint", "redact_secrets"]

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


_AUTH_401 = (
    "401 — ключ не принят именно этим адресом: GLM_API_KEY и GLM_BASE_URL должны принадлежать "
    "одному сервису; после правки .env контейнер нужно пересоздать"
)

#: Бизнес-коды z.ai/bigmodel (docs.bigmodel.cn/cn/api/api-code): они конкретнее HTTP-статуса,
#: поэтому идут первыми — «429» само по себе не отличает «кончился баланс» от «слишком быстро».
_ZAI_CODES: tuple[tuple[str, str], ...] = (
    ("1000", "аутентификация не прошла: ключ неверный/отозван или в нём пробел"),
    ("1001", "запрос ушёл без заголовка Authorization: GLM_API_KEY пуст"),
    ("1002", "ключ передан неверно: проверь, что в GLM_API_KEY нет кавычек и переноса строки"),
    ("1003", "ключ просрочен — перевыпусти его в консоли провайдера"),
    ("1004", "проверка ключа не пройдена: перевыпусти ключ и пересоздай контейнер"),
    ("1110", "аккаунт провайдера неактивен: проверь статус консоли"),
    (
        "1113",
        "на балансе нет средств — пополни счёт провайдера (у Coding Plan кошелёк отдельный)",
    ),
    ("1211", "провайдер не знает такую модель — сверь MODEL_* с его списком"),
    ("1210", "провайдер отверг параметры запроса: сообщения, TOOLS или нестандартный параметр"),
    ("1261", "промпт длиннее лимита модели: сократи контекст/историю"),
    ("1301", "вход или ответ отклонены модерацией провайдера"),
    ("1302", "слишком много одновременных запросов: снизь параллельность"),
    ("1303", "частота запросов выше лимита: пауза или другой ключ"),
    ("1304", "дневной лимит вызовов исчерпан"),
    ("1305", "сработал flow control провайдера: подожди"),
    ("1308", "достигнут лимит окна использования — сбросится по расписанию"),
    ("1310", "недельный/месячный лимит исчерпан: лимит сбросится позже"),
    ("1311", "текущий тариф/план не даёт доступа к этой модели — сними её в MODEL_*"),
    ("1312", "модель перегружена у провайдера: попробуй позже или другую роль"),
    ("1313", "fair-use: провайдер ограничил частоту для этого аккаунта"),
)

#: Порядок важен: от специфичного к общему. Ключ — подстрока в repr исключения: openai-ошибки
#: приходят как 'AuthenticationError: Error code: 401 - {...}'. Числовые коды сопоставляются по
#: границе числа, иначе «1000» ловится на «max_tokens: 10000».
_RULES: tuple[tuple[str, str], ...] = (
    *_ZAI_CODES,
    (
        "authenticationerror",
        _AUTH_401,
    ),
    ("401", _AUTH_401),
    (
        "permissiondeniederror",
        "403 — ключ принят, но доступа нет: тариф/имя модели, а у OpenRouter это ещё и "
        "отклонённый модерацией вход",
    ),
    (
        "moderation",
        "модерация провайдера отклонила вход: убери из контекста то, что не должно уходить наружу",
    ),
    (
        "403",
        "403 — ключ принят, но доступа к этой модели/запросу нет: тариф, права ключа, модерация",
    ),
    (
        "notfounderror",
        "404 — провайдер не нашёл путь или имя модели: у z.ai база .../api/paas/v4/, "
        "у OpenRouter .../api/v1 (и id там с префиксом автора)",
    ),
    ("404", "404 — неверный путь запроса или неизвестное имя модели: сверь GLM_BASE_URL и MODEL_*"),
    (
        "ratelimiterror",
        "429 — сработал лимит частоты/квоты: подожди минуту или переведи роль на flash-модель",
    ),
    ("429", "429 — сработал лимит частоты/квоты: подожди минуту"),
    ("content_filter", "провайдер отклонил запрос по файлу содержимого (его модерация)"),
    ("insufficient_balance", "на балансе ключа нет средств — пополни аккаунт провайдера"),
    ("insufficient_quota", "квота ключа исчерпана"),
    (
        "model not found",
        "провайдер не знает такую модель — сверь MODEL_BRAIN/MODEL_FAST/MODEL_VISION с его списком",
    ),
    ("invalid model", "провайдер не знает такую модель — сверь MODEL_* с его списком"),
    (
        "valid model",
        "провайдер не знает такое имя: у OpenRouter id с префиксом автора (z-ai/glm-5.2), "
        "у z.ai — без префикса (glm-4.7-flash)",
    ),
    (
        "reasoning is mandatory",
        "этот хост не даёт выключать размышление: LLM_THINKING_PARAM=false, экономия — ролью fast",
    ),
    (
        "insufficient credits",
        "402 — на аккаунте/ключе не хватает кредитов: пополни баланс провайдера "
        "(при отрицательном балансе отказывают даже :free-модели)",
    ),
    ("requires more credits", "402 — не хватает кредитов: пополни баланс провайдера"),
    ("402", "402 — не хватает кредитов на аккаунте провайдера"),
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


#: Формат ключа говорит, кто его выдал. Проверка нужна, потому что текст ответа провайдера об
#: этом молчит, а «ключ не принят» при живом ключе — почти всегда ключ от соседнего сервиса:
#: ZenMux выдаёт sk-ai-v1-/sk-ss-v1-/sk-cs-v1-, OpenRouter — sk-or-v1-, а z.ai/bigmodel — пару
#: <32 hex>.<16 символов>. Путаница дорогая: sk-ai-v1- выглядит как «ключ AI от z.ai», но это
#: именно ZenMux — на api.z.ai с таким ключом 401.
_ISSUER_PREFIXES: tuple[tuple[str, str, str, str], ...] = (
    ("sk-ai-v1-", "ZenMux", "https://zenmux.ai/api/v1/", ""),
    ("sk-ss-v1-", "ZenMux", "https://zenmux.ai/api/v1/", "ключи подписки и pay-as-you-go разные"),
    ("sk-cs-v1-", "ZenMux", "https://zenmux.ai/api/v1/", ""),
    (
        "sk-or-v1-",
        "OpenRouter",
        "https://openrouter.ai/api/v1/",
        "нужны кредиты на балансе OpenRouter",
    ),
)
#: Ключ z.ai / bigmodel.cn: 32 hex-символа, точка, 16 алфавитно-цифровых.
_ZAI_KEY = re.compile(r"^[0-9a-f]{32}\.[A-Za-z0-9]{16}$")
_ZAI_URL = "https://api.z.ai/api/paas/v4/"
_ZAI_NOTE = (
    "у z.ai два раздельных аккаунта: api.z.ai (международный) и open.bigmodel.cn (Китай) — "
    "ключ от одного к другому не переносится"
)

_ISSUER_HOSTS = {
    "z.ai": ("api.z.ai", "open.bigmodel.cn", "bigmodel.cn"),
    "ZenMux": ("zenmux.ai",),
    "OpenRouter": ("openrouter.ai",),
}


def issuer_of(api_key: str) -> tuple[str, str, str, str] | None:
    """(издатель, ожидаемый base_url, подсказка, форма ключа) по префиксу или маске."""
    key = (api_key or "").strip()
    if not key:
        return None
    for prefix, issuer, url, note in _ISSUER_PREFIXES:
        if key.startswith(prefix):
            return issuer, url, note, f"префиксом {prefix}…"
    if _ZAI_KEY.match(key):
        return "z.ai", _ZAI_URL, _ZAI_NOTE, "форматом <32 hex>.<16 символов>"
    return None


def _host_of(base_url: str) -> str:
    return (base_url or "").split("//")[-1].split("/")[0].casefold()


def auth_hint_for(*, base_url: str, api_key: str) -> str:
    """«Ключ выдан не этим сервисом» — одна строка, без сети. Пусто, если сказать нечего."""
    host = _host_of(base_url)
    known = issuer_of(api_key)
    if not host or known is None:
        return ""
    issuer, expected_url, note, shape = known
    if any(needle in host for needle in _ISSUER_HOSTS[issuer]):
        return ""
    extra = f" {note}." if note else ""
    return (
        f"ключ с {shape} выдан {issuer}, а запрос уходит на {host}: это разные сервисы."
        f"{extra} Подставь ключ {issuer} в GLM_API_KEY либо укажи GLM_BASE_URL={expected_url}"
    )


def gateway_auth_hint(gateway: object) -> str:
    """То же через шлюз; у тестовых заглушек метода может не быть — это не ошибка."""
    fn = getattr(gateway, "auth_hint", None)
    if not callable(fn):
        return ""
    return str(fn() or "")


_DIGITS = re.compile(r"^\d+$")


def _matches(text: str, needle: str) -> bool:
    """Подстрока для слов, граница числа — для кодов ошибок."""
    if _DIGITS.match(needle):
        return re.search(rf"(?<!\d){needle}(?!\d)", text) is not None
    return needle in text


def diagnose(error: str, *, timeout_s: float = 60.0, context: str = "") -> str:
    """Одна короткая строка с причиной и первым действием.

    Пустой вход — частый случай, когда исключение прилетело не от OpenAI-совместимого
    клиента: тогда честно говорим, что классифицировать нечего.
    """
    clean = redact_secrets(error or "").strip()
    extra = f" · {context.strip()}" if context and context.strip() else ""
    if not clean:
        return "провайдер не дал деталей; смотри `aegis doctor`" + extra
    low = clean.lower()
    for needle, remedy in _RULES:
        if _matches(low, needle):
            return remedy.format(timeout_s=timeout_s) + extra
    # классифицировать нечем, но текст может быть полезен: отдаём замаскированный кусок
    return f"ошибка провайдера: {clean[:120]}" + extra
