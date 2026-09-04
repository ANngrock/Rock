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

    # --- модели (OpenAI-совместимый API) ---
    glm_api_key: SecretStr | None = None
    glm_base_url: str = "https://api.z.ai/api/paas/v4/"
    model_brain: str | None = None
    model_vision: str | None = None
    model_fast: str | None = None
    model_embed: str | None = None
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

    # --- деньги / приватность / поведение агента ---
    timezone: str = "Europe/Moscow"
    base_currency: str = "RUB"
    daily_budget_usd: float = Field(default=2.0, gt=0)
    # размерность колонки knowledge.notes.embedding / memory.facts.embedding (миграция 0001).
    # 2048 — потому что embedding-3 без parameters отдаёт именно столько; ANN-индекс pgvector
    # для такой размерности недоступен (лимит 2000), см. комментарий в миграции.
    embedding_dims: int = Field(default=2048, ge=1)
    auto_allow_low_risk: bool = True
    pending_ttl_seconds: int = Field(default=3600, ge=30, le=86400)
    history_ttl_seconds: int = Field(default=86400, ge=60)
    history_limit: int = Field(default=30, ge=2, le=200)
    max_iterations: int = Field(default=8, ge=1, le=32)
    image_max_side: int = Field(default=1568, ge=256, le=4096)
    image_jpeg_quality: int = Field(default=85, ge=40, le=100)

    # --- наблюдаемость ---
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        if v not in available_timezones():  # падение на старте лучше падения в первом запросе
            raise ValueError(f"неизвестная TZ '{v}' (см. zoneinfo)")
        return v

    @field_validator("glm_base_url", "fallback_base_url")
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
