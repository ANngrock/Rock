"""Конфигурация — единственная точка входа для внешних настроек.

Правила:

* импорт пакета не должен падать без ``.env`` — обязательность полей проверяется
  :meth:`Settings.require_runtime` (её вызывают ``aegis bot``/``aegis doctor``), а не
  на уровне импорта модуля. Это позволяет запускать тесты и линтеры в чистом checkout;
* пустые значения в env (``TELEGRAM_OWNER_ID=``) считаются «не задано», а не ошибкой;
* секреты — только ``SecretStr``: случайный лог не распечатает ключ.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo, available_timezones

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["ConfigError", "Settings", "override_settings", "settings"]


class ConfigError(RuntimeError):
    """Конфигурация неполная — бот не стартует, но CLI/тесты живут."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- общий режим ---
    env: str = "dev"

    # --- Telegram: владелец один, все остальные игнорируются ---
    telegram_bot_token: SecretStr | None = None
    telegram_owner_id: int | None = None
    telegram_alerts_chat_id: int | None = None
    #: досылать ответ по мере генерации: то же сообщение правится по таймеру вместо «подожди 40
    #: секунд и держись за стул». По умолчанию выключено: редактирование — отдельный вызов API, и
    #: при чужих лимитах лучше молча отдать один готовый текст, чем воевать с 429 посреди ответа
    stream_replies: bool = False
    #: как часто можно править живое сообщение. Лимит Telegram считается на чат и делится со всеми
    #: вызовами, поэтому «редактировать на каждый токен» — это гарантированный отказ
    stream_edit_interval_ms: int = Field(default=900, ge=200, le=10_000)

    # --- модели (OpenAI-совместимый API) ---
    glm_api_key: SecretStr | None = None
    glm_base_url: str = "https://api.z.ai/api/paas/v4/"
    model_brain: str | None = None
    model_vision: str | None = None
    model_fast: str | None = None
    model_embed: str | None = None
    #: модель карантина (dual-LLM): снимает структуру с внешнего текста, планировщик его не видит
    model_quarantine: str | None = None
    #: Отдельный эндпоинт/ключ для эмбеддингов. Роутеры часто проксируют только chat, поэтому
    #: индексатор (шаг 2) должен уметь ходить за векторами напрямую в z.ai, не меняя провайдера
    #: всего остального. Пусто = используем основной glm_api_key/glm_base_url.
    embed_api_key: SecretStr | None = None
    embed_base_url: str | None = None
    fallback_api_key: SecretStr | None = None
    fallback_base_url: str | None = None
    fallback_model: str | None = None
    llm_timeout_s: float = 60.0
    #: Некоторые роутеры отклоняют нестандартный параметр thinking в теле запроса (400). Если после
    #: смены провайдера ответы пропали, а `doctor --models` показывает 400 — выставьте false.
    llm_thinking_param: bool = True
    #: Форма нестандартного параметра размышления: z.ai ждёт `thinking: {type: enabled|disabled}`,
    #: OpenRouter — `reasoning: {enabled: bool}`. auto выбирает по хосту GLM_BASE_URL.
    llm_thinking_style: Literal["auto", "zai", "openrouter"] = "auto"
    #: Глубина рассуждения, если провайдер её принимает (GLM 5.x: low/medium/high/max). Пусто =
    #: параметр не отправляется вовсе. Смысл менять только вместе с thinking=true: при деградации
    #: уровня 1 размышление выключается, и глубина не нужна.
    llm_reasoning_effort: Literal["", "low", "medium", "high", "max"] = ""
    llm_retries_per_client: int = Field(default=2, ge=0, le=6)
    llm_backoff_s: float = Field(default=1.5, ge=0.05, le=30.0)

    # --- инфраструктура ---
    database_url: str = "postgresql+asyncpg://aegis:aegis@localhost:5432/aegis"
    db_pool_size: int = Field(default=10, ge=1, le=100)
    redis_url: str = "redis://localhost:6379/0"
    #: "memory" — история диалога, pending-подтверждения и счётчик дневного бюджета живут
    #: только в этом процессе: для демо, CI и первого «пощупать», когда Redis поднимать лень.
    #: В prod запрещено: два процесса (бот и будущий воркер) увидят разные миры, а рестарт
    #: обнулить бюджет — то есть ровно то, за чем Redis тут и нужен.
    kv_backend: Literal["redis", "memory"] = "redis"
    nats_url: str = "nats://nats:4222"
    searxng_url: str = "http://localhost:8888"
    searxng_timeout_s: float = Field(default=20.0, ge=1.0, le=120.0)
    fetch_timeout_s: float = Field(default=25.0, ge=1.0, le=120.0)
    fetch_max_chars: int = Field(default=6000, ge=500, le=40000)
    fetch_allow_private: bool = False  # только для отладки против локальных сервисов

    # --- поиск и курсы (внешний мир) ---
    #: Порядок движков веб-поиска через запятую: searxng,zai. Пусто = поиск выключен, и бот скажет
    #: об этом прямо. SearXNG идёт первым по ADR-007 (запросы не уходят наружу), zai — резерв тем же
    #: ключом, что и модели: когда Google режет датацентровые IP, SearXNG отвечает 200 и нулём
    #: результатов, и без второго движка владелец получает «уточните запрос» вместо ответа.
    search_engines: str = "searxng,zai"
    search_timeout_s: float = Field(default=15.0, ge=1.0, le=120.0)
    #: «сегодня/курс/новости» сначала ищем с суточным окном; если пусто — второй заход без него.
    search_freshness_first: bool = True
    #: Одна повторная попытка исходной формулировкой: наша «оптимизация» запроса иногда и портит.
    search_refine: bool = True
    #: Движок z.ai (POST {base}/web_search). search-prime — премиум-индекс; список допустимых имён
    #: отдаёт docs.z.ai/api-reference/tools/web-search.
    search_zai_engine: str = "search-prime"
    search_zai_base_url: str | None = None
    #: Цена вызова поиска: у z.ai в прайсе она не указана — заведите число, и вызовы поиска пойдут
    #: в дневной бюджет. 0 = не учитывать (тогда SearXNG-дефолт бесплатен честно).
    search_cost_usd_per_call: float = Field(default=0.0, ge=0.0, le=5.0)

    #: Курсы валют — детерминированный путь без LLM (принцип «парсеры работают всегда»).
    #: Источники без ключей: privatbank (касса+безналичный), nbu (официальный), erapi (агрегатор).
    rate_sources: str = "privatbank,nbu,erapi"
    rates_timeout_s: float = Field(default=10.0, ge=1.0, le=60.0)
    #: Сколько минут ответ о курсе считается годным для повтора. Истина всё равно в источнике.
    rate_cache_ttl_s: int = Field(default=300, ge=0, le=3600)
    #: Расхождение между источниками в пределах допуска = «согласуются»; выше — показываем разброс.
    rate_tolerance_pct: float = Field(default=1.5, ge=0.05, le=25.0)
    #: Валюта, к которой котируют банки (UAH для Привата/НБУ). Не путать с BASE_CURRENCY учёта.
    rate_home_currency: str = "UAH"

    # --- деньги / приватность / поведение агента ---
    timezone: str = "Europe/Moscow"
    base_currency: str = "RUB"
    daily_budget_usd: float = Field(default=2.0, gt=0)
    # размерность колонки knowledge.notes.embedding / memory.facts.embedding (миграция 0001).
    # 2048 — потому что embedding-3 без parameters отдаёт именно столько; ANN-индекс pgvector
    # для такой размерности недоступен (лимит 2000), см. комментарий в миграции.
    embedding_dims: int = Field(default=2048, ge=1)
    #: заметок за один проход индексатора: очередь может быть длиннее (первый запуск после
    #: импорта), и лучше несколько проходов по 200, один из которых не съест таймаут сервиса
    embed_index_limit: int = Field(default=200, ge=1, le=5000)
    #: текстов в одном запросе к /embeddings — столько же, сколько принимает провайдер
    embed_batch_size: int = Field(default=32, ge=1, le=128)
    #: сколько символов заметки уходит в эмбеддинг: хвост длинной сохранённой страницы —
    #: мусор, а лимит входа у модели конечный
    embed_max_chars: int = Field(default=2000, ge=100, le=32_000)
    auto_allow_low_risk: bool = True
    pending_ttl_seconds: int = Field(default=3600, ge=30, le=86400)
    history_ttl_seconds: int = Field(default=86400, ge=60)
    history_limit: int = Field(default=30, ge=2, le=200)
    max_iterations: int = Field(default=8, ge=1, le=32)
    image_max_side: int = Field(default=1568, ge=256, le=4096)
    image_jpeg_quality: int = Field(default=85, ge=40, le=100)

    # --- воспроизводимость (M1): журнал решений с хэш-цепочкой ---
    #: Писать decision records. Выключить имеет смысл только если БД живёт на медленном диске и
    #: журнал мешает: ответы не зависят от него, а «докажите, почему тогда так ответили» — да.
    repro_enabled: bool = True
    #: Содержимое запросов/ответов модели в блобы. Без него остаются только метрики (как в аудите),
    #: и replay/«почему ты так ответил» становится невозможен — это единственный по-настоящему
    #: дорогой по объёму переключатель.
    repro_record_payload: bool = True
    #: Потолок одного блоба. Больше — храним начало и честно помечаем запись как truncated.
    repro_max_blob_bytes: int = Field(default=1_048_576, ge=4096, le=33_554_432)
    #: Сколько записей сверяет `aegis repro verify` за один проход по умолчанию.
    # ------------------------------------------------- верификация ответа и карантин (шаг 2)
    #: сверять ли ответы с результатами инструментов, прежде чем показать их владельцу
    verify_enabled: bool = True
    #: сверять и ответы без чисел: выдуманный «качественный» факт числами не ловится, только судьёй
    verify_always: bool = False
    #: короткие ответы не проверяем: там нечего сверять, а платный вызов — будет
    verify_min_answer_chars: int = Field(default=200, ge=0)
    #: сколько источников отдавать судье (по 3000 знаков на каждый)
    verify_max_sources: int = Field(default=6, ge=1, le=20)
    #: размечать ли внешний текст отдельной моделью, чтобы планировщик не читал сырьё
    quarantine_enabled: bool = True
    #: короткие вставки (строка курса, один заголовок) размечать дороже, чем читать: порог смысла
    quarantine_min_chars: int = Field(default=600, ge=0)
    #: сколько сырых знаков вообще отдаётся карантину — защита от «перескажи мне весь интернет»
    quarantine_max_chars: int = Field(default=24000, ge=1000)

    # --- напоминания ---
    #: выключено = инструмент честно отказывает; «поставил, но не сработает» хуже, чем «не могу»
    reminders_enabled: bool = True
    #: сколько строк расписания уносит один тик: после простоя (или --persistent) очередь может
    #: быть длинной, и выплюнуть её всю за раз — значит засыпать владельца сообщениями
    reminders_batch: int = Field(default=20, ge=1, le=200)

    repro_verify_limit: int = Field(default=5000, ge=100, le=200_000)

    # --- наблюдаемость ---
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        if v not in available_timezones():  # падение на старте лучше падения в первом запросе
            raise ValueError(f"неизвестная TZ '{v}' (см. zoneinfo)")
        return v

    @field_validator("glm_base_url", "fallback_base_url", "search_zai_base_url")
    @classmethod
    def _normalize_base_url(cls, v: str | None) -> str | None:
        """Открытый чат с собой: в .env регулярно прилетает полный URL эндпоинта.

        OpenAI-клиент сам дописывает ``chat/completions``, поэтому ``.../v4/chat/completions``
        превращается в 404, а выглядит как «провайдер не ответил». Срезаем и хвостовой слэш,
        и явный путь эндпоинта; trailing slash приводим к единому виду.
        """
        if v is None:
            return None
        cleaned = v.strip().rstrip("/")
        for suffix in ("/chat/completions", "/responses", "/completions", "/embeddings"):
            if cleaned.endswith(suffix):
                cleaned = cleaned[: -len(suffix)]
                break
        return f"{cleaned}/"

    @field_validator("search_engines", "rate_sources")
    @classmethod
    def _check_csv(cls, v: str) -> str:
        """Список через запятую: пробелы, регистр и повторы не должны менять поведение."""
        parts: list[str] = []
        for item in (v or "").split(","):
            name = item.strip().casefold()
            if name and name not in parts:
                parts.append(name)
        return ",".join(parts)

    @field_validator("rate_home_currency")
    @classmethod
    def _check_rate_home(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError("RATE_HOME_CURRENCY должен быть ISO-4217, напр. UAH")
        return v

    @field_validator("base_currency")
    @classmethod
    def _check_currency(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError("BASE_CURRENCY должен быть ISO-4217, напр. RUB")
        return v

    @model_validator(mode="before")
    @classmethod
    def _drop_empty_env_values(cls, data: Any) -> Any:
        """``FOO=`` в .env means «не задано», а не «пустая строка»."""
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not (isinstance(v, str) and not v.strip())}
        return data

    # --- derived ---
    @property
    def is_production(self) -> bool:
        return self.env.lower() in {"prod", "production"}

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def missing_runtime_keys(self) -> list[str]:
        """Ключи, без которых бот бессмысленен."""
        missing: list[str] = []
        if self.telegram_bot_token is None:
            missing.append("TELEGRAM_BOT_TOKEN")
        if self.telegram_owner_id is None:
            missing.append("TELEGRAM_OWNER_ID")
        if self.glm_api_key is None:
            missing.append("GLM_API_KEY")
        return missing

    def require_runtime(self) -> Self:
        missing = self.missing_runtime_keys()
        if missing:
            raise ConfigError(
                "не хватает настроек: "
                + ", ".join(missing)
                + ". Скопируйте .env.example в .env и заполните."
            )
        if self.is_production and self.kv_backend != "redis":
            raise ConfigError(
                "ENV=prod не может работать с kv_backend="
                + self.kv_backend
                + ": состояние сессий и дневной бюджет обязаны переживать рестарт процесса"
            )
        return self

    def redacted(self) -> dict[str, Any]:
        """Для логов/doctor: секреты заменены на ***."""
        out: dict[str, Any] = {}
        for name, value in self.model_dump(mode="json").items():
            fields = type(self).model_fields[name]
            ann = str(fields.annotation)
            if "SecretStr" in ann:
                out[name] = "***" if value not in (None, "") else None
            else:
                out[name] = value
        return out


# --- доступ к конфигурации -------------------------------------------------------------

_OVERRIDDEN: Settings | None = None
_CACHED: Settings | None = None


def settings() -> Settings:
    """Кешированный синглтон. Импорты не падают без ``.env`` — валидация на ``require_runtime``."""
    if _OVERRIDDEN is not None:
        return _OVERRIDDEN
    global _CACHED
    if _CACHED is None:
        _CACHED = Settings()
    return _CACHED


@contextmanager
def override_settings(**values: Any) -> Iterator[Settings]:
    """Точечная подмена конфигурации в тестах и dev-сессиях (значение возвращается после)."""
    global _OVERRIDDEN
    base = settings()
    merged = base.model_dump() | values
    _OVERRIDDEN = Settings(**merged)
    try:
        yield _OVERRIDDEN
    finally:
        _OVERRIDDEN = None


def reload_settings() -> Settings:
    """Перечитать env (после смены ``.env`` в dev-сессии)."""
    global _CACHED
    _CACHED = None
    return settings()
