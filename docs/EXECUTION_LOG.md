# EXECUTION LOG

Журнал исполнения дорожной карты. Формат: пункт плана → статус → чем именно закрыт → отклонения
от «бумажного» шага (они важны: код в PART II плана был черновиком, местами нерабочим).

Обозначения: ✅ сделано и проверено · 🟡 сделано, но ждёт действия владельца (нужен Docker/токены) · ⏳ не начато.

---

## Шаг 1 — Фундамент

| Пункт | Статус | Чем закрыто |
|---|---|---|
| 1.1 Инфраструктура | ✅ | `deploy/docker-compose.yml` (pgvector/pg16, redis:7 AOF, searxng, nats+temporal под профилем `durable`, сервис бота), `deploy/Dockerfile` (python:3.12-slim, tini, ffmpeg, uid 10001, HEALTHCHECK = `aegis doctor`), `Makefile`, `.env.example`, `.gitignore`, `.dockerignore` |
| 1.2 Платформа: config/db/event store | ✅ | `platform/config.py` (Settings + `missing_runtime_keys()`/`require_runtime()`/`redacted()`), `platform/db.py` (ленивый engine), `platform/events/store.py` (append-only + transactional outbox, `fetch_unpublished` через `SKIP LOCKED`), `platform/kv.py` (узкий порт под redis), `platform/logging.py` (structlog, stderr) |
| 1.3 Model Gateway | ✅ | `platform/gateway/{models,dlp,cost,client}.py`: каталог ролей, retry/backoff, смена провайдера на 400, fallback, бюджетный гейт + деградация (thinking off → fast), DLP mask/unmask, запись каждого вызова (успешного и нет) в `platform.llm_calls` |
| 1.4 Governance | ✅ | `governance/policy.py` (allow/confirm/deny — решает политика, не промпт), `governance/audit.py` (`tool_runs`, `llm_calls`), `governance/killswitch.py` |
| 1.5 Tool registry + инструменты | ✅ | `agents/tools/registry.py` (Pydantic → OpenAI schema, проверка имени, `writes`/`risk`), `agents/tools/builtin.py` — 10 инструментов: `get_datetime, remember_fact, list_facts, forget_fact, add_note, search_notes, web_search, fetch_page, save_link, analyze_image`; `tools/images.py` (даунскейл перед vision) |
| 1.6 Supervisor v1 | ✅ | `agents/supervisor.py`: tier-роутинг (smalltalk → `fast` без инструментов; вложения → `brain`+tools; подсказка анализа → thinking), цикл tool calling с лимитом итераций, pending-подтверждения в Redis, `resume()` по `tool_call_id`, `status()`, деградация без LLM/БД/бюджета |
| 1.7 Telegram | ✅ | `interaction/telegram/bot.py` + `render.py`: owner-whitelist мидлварой, команды `/start /help /new /cost /status /tools /halt /resume`, inline-кнопки подтверждения, фото/документы→vision, HTML-санитайзер + чанкер, фолбэк в plain text при 400 |
| 1.8 Миграция 0001 | ✅ | `migrations/versions/0001_foundation.py`: схемы `platform/governance/memory/knowledge`, расширения `vector/pg_trgm/pgcrypto`, таблицы `events/outbox/llm_calls/tool_runs/facts/notes`, триггер append-only, `v_cost_daily`, HNSW + trgm индексы, `downgrade()` |
| 1.9 Тесты и CI | ✅ | 147 тестов (139 юнит + 8 интеграционных под `-m integration`), property-тесты DLP на hypothesis, `evals/golden_v1.jsonl` (23 кейса) + `evals/run_golden.py`, `.github/workflows/ci.yml` (static → integration с pgvector/redis → сборка образа) |
| 1.10 Запуск у владельца | 🟡 | код и инструкция готовы (`README.md`, `docs/RUNBOOK.md`): `make up && make migrate && make doctor`. В песочнице нет Docker — фактический прогон делает владелец |
| 1.11 Бэкапы | 🟡 | `deploy/backup.sh` (`backup`/`verify`/`restore`/`list`, sha256, ротация, rclone), systemd-таймер `deploy/systemd/aegis-backup.*`, cron-строка в RUNBOOK. Требование «проверка восстановления» реализована как `make backup-verify` — именно она была слабым местом пункта. Прогон — на машине владельца |

### После первичного прогона гейтов (дополнение к шагу 1)

- `KV_BACKEND=memory` + `platform/kv_memory.py`: бот поднимается вообще без Redis — для демо, CI и «пощупать за 5 минут». В `ENV=prod` запрещено (`require_runtime` отказывается стартовать): история/pending/дневной бюджет обязаны переживать рестарт.
- Устойчивость к отказу кэша (принцип 5): `Supervisor` больше не падает, если Redis лёг — история хода деградирует до «без истории», `CostGovernor` ведёт локальный счётчик и берёт `max(redis, локальный)`, а постановка действия на подтверждение при недоступном хранилище даёт честный отказ вместо висящей кнопки. Тесты: `tests/test_kv_memory.py` (8 кейсов, включая «запись не выполнилась без подтверждения»). В `/status` появился флаг `kv_degraded`.
- Старт бота стал диагностируемым: неполный конфиг и сетевые ошибки Telegram (включая перехват TLS в песочницах) печатаются одной строкой с подсказкой вместо трейсбека, коды выхода 2 (конфиг) и 3 (сеть/токен).

### Отклонения от PART II плана (и почему)

1. **`engine = create_async_engine(settings().database_url)` на уровне импорта** — убрано. Импорт
   пакета не должен требовать БД: иначе `aegis tools`, `doctor`, тесты и alembic падали до старта.
   Engine/sessionmaker теперь ленивые (`platform/db.py`), с `reset_engine()` для тестов.
2. **`@lru_cache settings()`** — заменено на явный кеш модуля + `override_settings()`; иначе
   тесты не могли переопределить конфигурацию, а `Settings` строился бы до загрузки `.env`.
3. **Append-only через `CREATE RULE ... DO INSTEAD NOTHING`** — заменён на триггер с
   `RAISE EXCEPTION`. Правило молча «съедает» UPDATE/DELETE, что выглядит как защита, но не даёт
   увидеть попытку правки; исключение же попадает в аудит и в алерт.
4. **`_loop`: при CONFIRM сообщения пушались без tool-ответа** — по спецификации OpenAI-совместимого
   API на каждый `tool_calls[].id` обязан быть `{"role":"tool"}`. Иначе после первого же
   подтверждения следующий запрос модели падал с 400. Сейчас: `CONFIRM_PLACEHOLDER` в момент
   ожидания, реальный результат — в `resume()`, отказ — `DENIED: <причина>`.
5. **DLP: обратное раскрытие в порядке прямоты** — токены вида `<CARD_1>` могли встречаться в
   пользовательском тексте и подменяться чужими значениями. Формат токена now
   `<AEGIS_PII:KIND:n>`, вход сначала экранируется как RAW (никаких «случайных» токенов),
   раскрытие — в обратном порядке.
6. **Регэкспы PII**: `CARD` не должен был съедать пробел после номера (ломал склейку слов в
   промпте), `PASSPORT` не ловил «45 09 123456» — обе поправлены, покрыты тестами и round-trip
   property-тестом.
7. **`CostGovernor(r: redis.Redis, ...)`** — принимает узкий порт `KV` (тот же контракт, что и
   `Supervisor`), поэтому тесты на `fakeredis`/фейке и реальный клиент взаимозаменяемы; TTL 3 суток
   вместо 1 (счётчик дня обязан пережить полночный рестарт контейнера).
8. **`ctx.services: dict`** — заменён на типизированный `Services` с портами
   (`gateway/facts/notes/kv/audit/events/kill_switch`). Это то, что сделало агентный цикл
   тестируемым без поднятых сервисов, и дало границы для import-linter.
9. **`Reply` без метрик** — добавлены `trace_id/model/cost_usd/iterations/degraded`, чтобы «всё
   воспроизводимо» (принцип 4) было видно не только в БД, но и в ответе/`aegis ask`.
10. **`F.photo` без ограничения размера** — добавлена подготовка изображения (EXIF-поворот,
    даунскейл по длинной стороне, JPEG): cost vision-модели растёт от разрешения, а «что это за
    фото» не должно стоить как полчаса текста.
11. **`OwnerOnly: user.id != cfg.telegram_owner_id`** — сравнение строк, отказ вместо исключения
    (падение мидлвари = бот молчит на все апдейты).
12. **`send_reply`: нарезка по 4000 символов** — грубая нарезка рвала теги и URL; теперь
    `render_for_telegram` (санитайз → чанковка сбалансированных тегов), а при 400 от Telegram —
    повтор отправкой plain text.
13. **`requires-python >= 3.12`** — в CI/докере 3.12 как в плане; локальная песочница подняла
    только 3.11 (`uv python install 3.12` бился о сертификаты), поэтому в `pyproject` стоит
    `>=3.11` и 3.12-синтаксис не используется. Откат, если понадобится 3.12-специфика, — одна
    строка в `pyproject`.
14. **Инструментов 10 вместо 7** — добавлены `list_facts`, `forget_fact`, `save_link`: без
    «прочитать/забыть» память превращалась в журнал, который нельзя поправить, а `save_link`
    закрывает самый частый сценарий «скинуть ссылку, чтобы разобрать позже».
15. **Немного больше, чем просил план**: CLI `aegis doctor --json` (+HEALTHCHECK контейнера),
    `aegis ask` (прогон supervisor без Telegram), SSRF-guard в `web/net.py`, офлайн-раннер
    golden-эвалов в CI, `make up-core`/`up-durable` (шаг 1 не обязан поднимать NATS/Temporal).

### Что реально проверяется сейчас (и как это повторить)

```bash
make fmt lint type imports     # ruff format/check, mypy --strict (44 модуля), 3 контракта import-linter
make test                      # 139 юнит-тестов, без сети и БД
python evals/run_golden.py       # 23/23 golden-кейсов: роутинг, политика, DLP, инъекции, рендер
pytest -m integration            # 8 тестов на реальной Postgres (нужен поднятый стек)
aegis doctor --quick --json      # конфиг + связности; без БД/Redis ожидаемо exit=1
```

Логику «внешний контент не командует» проверяют три вещи: `web_search`/`fetch_page` всегда
оборачивают результат в `<untrusted>` и нейтрализуют преждевременный `</untrusted>`; политика
поднимает `CONFIRM` для записи из untrusted-источника; `save_link`/`add_note` пишут только то, что
разрешил владелец.

---

## Шаг 2 — Ядро агента 2.0 (следующий; начинать после 1.11)

- ⏳ Temporal workflow `AgentSession`: activity = `llm_call`/`tool_run`, сигнал `confirm` вместо
  pending в Redis (порт Supervisor к этому уже подготовлен: `route()` и цикл принимают уровень
  деградации извне)
- ⏳ Outbox-relay → NATS JetStream + первый подписчик: индексатор эмбеддингов (после этого
  `search_notes` работает векторно, а не только ILIKE/tsquery)
- ⏳ Verifier для tier-2: чистый контекст, проверка чисел через инструменты
- ⏳ Dual-LLM: quarantine-модель извлекает структуру из `<untrusted>`, мозг видит только структуру
- ⏳ STT: faster-whisper (ogg→wav через ffmpeg; ffmpeg в образе уже есть)
- ⏳ Напоминания: `schedule_reminder` + Temporal timer
- ⏳ Langfuse self-hosted: трассы уже содержат `trace_id`/`model`/токены/стоимость — останется
  добавить экспортёр
- ⏳ Golden-наборы v2: даты/финансы/инъекции поверх текущих 23 кейсов
