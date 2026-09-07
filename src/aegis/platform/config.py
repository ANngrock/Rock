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
    #: relay включается явно: NATS живёт в compose-профиле `durable`, и на большинстве установок
    #: его нет. «Выключено» означает «события копятся в outbox» — это безопасно, а не потеряно:
    #: публикация догонит, когда транспорт появится
    outbox_relay_enabled: bool = False
    #: событий за один тик: строки держатся `FOR UPDATE` до commit'а, поэтому пакет обязан быть
    #: коротким — иначе следующий тик будет ждать locks вместо работы
    outbox_batch: int = Field(default=50, ge=1, le=1000)
    #: после скольких неудач строка перестаёт выбираться выборкой (остаётся в очереди до починки)
    outbox_max_attempts: int = Field(default=8, ge=1, le=100)
    #: JetStream-стрим: `ack` от сервера — единственное, что делает «опубликовано» честным
    nats_stream: str = "aegis_events"
    #: префикс субъектов: `<prefix>.<stream_type>.<stream_id>.<event_type>`
    nats_subject_prefix: str = "aegis"
    nats_connect_timeout_s: float = Field(default=3.0, ge=0.5, le=30.0)

    # --- Экспорт журнала в Langfuse (OTLP/HTTP): витрина над журналом, а не второй журнал ---
    #: выключено по умолчанию: наблюдение не обязательно для работы бота, а ключи уезжают наружу
    langfuse_enabled: bool = False
    #: базовый адрес без пути: https://cloud.langfuse.com или http://langfuse:3000
    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_timeout_s: float = Field(default=10.0, ge=0.5, le=120.0)
    #: символов input/output в спане: журнал хранит полное содержимое, витрина — нет
    langfuse_max_chars: int = Field(default=6_000, ge=500, le=100_000)
    #: окно перечитывания: повтор безопасен (id выводятся из записей), поэтому берём с запасом
    langfuse_window_hours: int = Field(default=24, ge=1, le=24 * 31)
    #: записей журнала за один прогон
    langfuse_limit: int = Field(default=500, ge=1, le=5_000)
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
    #: как часто ход расписания делает сам процесс бота: 0 = только systemd-таймер (±5 минут),
    #  >0 = тик в процессе — звонку нужно «ровно в 15:00», а не «когда таймер спохватится»
    reminders_inprocess_seconds: int = Field(default=60, ge=0, le=3600)

    # --- внешние подключения (MCP / API-ключи / плагины) ---
    #: выключено = реестр не читается и инструменты не регистрируются; данные подключений
    #: при этом остаются на месте (отключить рубильником ≠ потерять конфиг)
    integrations_enabled: bool = True
    mcp_call_timeout_seconds: int = Field(default=30, ge=3, le=300)
    mcp_max_tools: int = Field(default=40, ge=1, le=200)

    # --- звонок как канал доставки напоминаний ---
    #: none|twilio|webhook. Twilio — TTS через REST и без публичного URL (TwiML передаётся телом
    #  запроса); webhook — POST {"to","text"} на свой шлюз (Asterisk/FreePBX/софтфон-мост)
    call_provider: str = "none"
    #: телефон владельца в E.164. Берётся из конфига, а не из фразы чата: номер, продиктованный
    #  сообщением, — открытая дверь «позвони куда скажут» для prompt-инъекции из веб-страницы
    notify_phone: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr | None = None
    twilio_from_number: str = ""
    call_webhook_url: str = ""
    #: таймаут = «дождаться принятия звонка провайдером», а не длительность разговора
    call_timeout_seconds: int = Field(default=12, ge=3, le=120)

    repro_verify_limit: int = Field(default=5000, ge=100, le=200_000)

    # --- наблюдаемость ---
    log_level: str = "INFO"
    log_json: bool = True

    # --- актив-актив (F1): один ход на владельца, сериализуемый БД, а не «мы же один процесс» ---
    #: минуты аренды открытого хода: упавший процесс освобождает место владельцу, не дожидаясь
    #: ручной расчистки. Дальше этого срока ход считается осиротевшим
    turn_lease_minutes: int = Field(default=15, ge=1, le=240)
    #: сколько отложенных ходов разбирать за один проход (drain): «не потерять второй апдейт»
    #: означает и «не устроить лавину» после простоя
    turn_drain_limit: int = Field(default=3, ge=1, le=16)

    # --- principals и RBAC (F2) ---
    #: telegram id членов семьи/гостей через запятую: `42:member,77:guest`. Пусто — владелец один,
    #: и гранты owner получает полностью (исторический режим «владелец и всё»)
    principal_roster: str = ""
    #: роль сессии для RLS: на проде — 'member'-подобные restricted-роли; superuser RLS обходит,
    #: и doctor обязан это показывать, а не делать вид, что изоляция работает
    rls_expected_role: str = "aegis_app"

    # --- конвертовое шифрование (F3) ---
    #: off — не шифровать (dev/тесты); auto — шифровать, если ключ задан; enforce —
    #: шифровать всегда, без ключа приложение не стартует. «Тихо не шифруем, потому что
    #: забыли ключ» — ровно тот
    #: режим, в котором блок «зашифрован» перестаёт значить что-либо
    crypto_mode: Literal["off", "auto", "enforce"] = "auto"
    #: активный KEK: base64 от 32 байт (env AEGIS_KEK). Для ротации старые версии читаются как
    #: AEGIS_KEK_V7 и т.д. — ключи в БД не лежат нигде
    crypto_kek: SecretStr | None = None
    crypto_key_version: int = Field(default=1, ge=1, le=10_000)
    #: размер батча rewrap-прохода: те же аренды, что у напоминаний, и тот же потолок боли
    rewrap_batch: int = Field(default=200, ge=10, le=5000)

    # --- политика как код (F5) ---
    #: декларативные правила; пусто/нет файла — встроенный набор (поведение до F5 не меняется)
    policy_rules_path: str = "deploy/policy/rules.yml"
    #: lock-файл предыдущей версии правил: гейт «ослабление без golden-кейса» сверяется с ним
    policy_lock_path: str = "deploy/policy/rules.lock.json"

    # --- флаги возможностей (F6) ---
    #: старше этого возраста флаг на 100% обязан быть удалён (CI-гигиена)
    flag_stale_days: int = Field(default=90, ge=7, le=365)
    #: как часто перечитывать определения флагов из БД (секунды)
    flags_cache_s: float = Field(default=30.0, ge=0.0, le=3600.0)

    # --- SLO и право на деградацию (F7) ---
    slo_path: str = "deploy/slo.yml"
    #: разрешено ли приложению переключать ветки (стриминг/веб-инструменты) автоматически
    slo_enforce_degradation: bool = True
    #: окно наблюдения SLO (минуты): короче — дребезг, длиннее — деградация приходит после инцидента
    slo_window_minutes: int = Field(default=60, ge=10, le=1440)
    #: как часто процесс сбрасывает снапшот метрик в platform.metric_samples (секунды)
    metrics_flush_seconds: int = Field(default=30, ge=5, le=3600)
    #: TTL автоматических переключателей деградации: «галочка без expiry» = деградация навсегда
    degradation_ttl_seconds: int = Field(default=900, ge=60, le=86400)

    # --- retention-as-code (F3) ---
    retention_file: str = "deploy/retention.yml"

    # --- воркфлоу хода (F8) ---
    #: off — старый путь (супервизор сам); local — тот же код шагов через локальный воркфлоу-раннер;
    #: temporal — Temporal-кластер. Отсутствие кластера — норма, не авария (ADR-0021)
    workflow_mode: Literal["off", "local", "temporal"] = "off"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "aegis"

    # --- качество поиска (F9) ---
    #: кандидатов на ветку перед fusion; reranker всегда смотрит не больше rerank_limit
    search_candidates: int = Field(default=40, ge=5, le=500)
    rerank_limit: int = Field(default=20, ge=1, le=100)
    #: off — только гибрид; lexical — детерминированный реранк; model — доп. вызов судьи (дорого)
    rerank_mode: Literal["off", "lexical", "model"] = "lexical"
    #: константа reciprocal-rank fusion; 60 — значение из оригинальной статьи, менять просто
    #: так нельзя
    rrf_k: int = Field(default=60, ge=1, le=1000)

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
        provider = (self.call_provider or "none").strip().lower()
        if provider not in {"none", "twilio", "webhook"}:
            raise ConfigError(
                f"CALL_PROVIDER={self.call_provider!r}: знаю только none|twilio|webhook"
            )
        if provider != "none" and not self.notify_phone.strip():
            raise ConfigError(
                f"CALL_PROVIDER={provider} требует NOTIFY_PHONE: звонить некому — "
                "лучше none, чем «напоминание сорвётся на середине»"
            )
        if provider == "twilio" and not (
            self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number
        ):
            raise ConfigError(
                "CALL_PROVIDER=twilio требует TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN и "
                "TWILIO_FROM_NUMBER (номер с голосовой поддержкой)"
            )
        if provider == "webhook" and not self.call_webhook_url.startswith("http"):
            raise ConfigError("CALL_PROVIDER=webhook требует CALL_WEBHOOK_URL (http/https)")
        if self.crypto_mode == "enforce" and self.crypto_kek is None:
            raise ConfigError(
                "CRYPTO_MODE=enforce требует AEGIS_KEK (base64, 32 байта). Иначе «зашифровано» "
                "означало бы «как повезёт с env» — enforce и есть способ не позволить этого"
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
