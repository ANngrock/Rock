# RUNBOOK — эксплуатация aegis (шаг 1)

Всё, что нужно, чтобы поднять, проверить и починить личную установку. Формула дежурства:
`make up` → `make migrate` → `make doctor` → бэкап по таймеру. Остальное — из этого файла.

## 1. Что где лежит

| Путь | Назначение |
|---|---|
| `deploy/docker-compose.yml` | postgres (pgvector), redis, searxng, nats, temporal, bot |
| `deploy/backup.sh` | `backup` / `verify` / `restore` / `list` |
| `deploy/systemd/aegis-backup.{service,timer}` | ежедневный бэкап + контрольное восстановление |
| `migrations/versions/0001_foundation.py` | вся схема шага 1 (schemas `platform/governance/memory/knowledge`) |
| `.env` | секреты и адресная часть конфигурации (не в гите) |

## 2. Первый запуск

```bash
cp .env.example .env
$EDITOR .env            # TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID, GLM_API_KEY — обязательны
make up                 # собирает образ и поднимает контейнеры
make migrate            # alembic upgrade head внутри контейнера бота
make doctor             # самодиагностика: конфиг + связности
docker compose -f deploy/docker-compose.yml logs -f bot
```

Проверка в Telegram (владелец, не вайтлист — молчание):

```
/status          модели, бюджет за день, версия промпта, состояние kill switch
запомни: рост 182   →  факт в memory.facts
найди в интернете курс EUR  →  web_search (SearXNG) → саммари
скинуть фото       →  analyze_image (vision-модель)
```

## 3. Конфигурация

Ключи (префикс `AEGIS_` не используется, имена — верхний регистр от полей `Settings`):

| Переменная | По умолчанию | Зачем |
|---|---|---|
| `ENV` | `dev` | `prod` включает строгую проверку конфига (`require_runtime`) |
| `TELEGRAM_BOT_TOKEN` | — | токен от @BotFather |
| `TELEGRAM_OWNER_ID` | — | единственный пользователь, которому бот отвечает |
| `TELEGRAM_ALERTS_CHAT_ID` | пусто | чат для алертов (сначала `/start` боту, id со знаком минус) |
| `GLM_API_KEY` | — | ключ OpenAI-совместимого провайдера: z.ai **или** роутера (ZenMux и т. п.) |
| `GLM_BASE_URL` | `https://api.z.ai/api/paas/v4/` | базовый URL **без** `/chat/completions` (хвост эндпоинта срезается автоматически, OpenAI-клиент дописывает сам) |
| `LLM_THINKING_PARAM` | `true` | `false` — не слать нестандартный `thinking` в теле: роутеры, которые его не знают, отвечают 400 |
| `MODEL_BRAIN` / `MODEL_VISION` / `MODEL_FAST` / `MODEL_EMBED` | каталог | имена моделей; цена **и возможности** берутся по имени (`PRICES`, `THINKING_ALWAYS_ON`) |
| `EMBED_BASE_URL` / `EMBED_API_KEY` | пусто = основной провайдер | куда ходить за векторами: чат-роутеры часто эмбеддинги не проксируют |
| `FALLBACK_API_KEY` / `FALLBACK_BASE_URL` / `FALLBACK_MODEL` | пусто | резервный провайдер при 5xx/лимите |
| `KV_BACKEND` | `redis` | `memory` — запуск вообще без Redis: демо/CI/первое «пощупать». Состояние живёт только в процессе, в `ENV=prod` запрещено |
| `DATABASE_URL` | `postgresql+asyncpg://aegis:aegis@postgres:5432/aegis` | хост = `postgres` (имя сервиса) вне контейнера заменить на `localhost` |
| `REDIS_URL` | `redis://redis:6379/0` | история, pending_actions, счётчик бюджета |
| `SEARXNG_URL` | `http://searxng:8080` | self-hosted поиск |
| `DAILY_BUDGET_USD` | `2.0` | жёсткий дневной лимит: 85 % → thinking off, затем только `fast` |
| `AUTO_ALLOW_LOW_RISK` | `true` | выключить = подтверждать даже низко-рисковые записи |
| `PENDING_TTL_SECONDS` | `3600` | сколько живёт кнопка подтверждения |
| `TIMEZONE` | `Europe/Moscow` | время в промпте и в `get_datetime` |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `true` | логи идут в stderr (stdout оставлен машинному выводу CLI) |

Пустой `.env` — не поломка: `make doctor` покажет `config_missing`, а `aegis bot` откажется стартовать с внятным сообщением.

## 4. Ежедневные команды

```bash
make ps        # состояние контейнеров
make logs      # логи бота (follow)
make restart   # перезапустить только бота
make migrate   # alembic upgrade head
make test      # юниты (не требуют БД)
make test-all  # + интеграции (требует поднятый стек)
make fmt       # ruff format + автофиксы
make lint      # ruff check + mypy + lint-imports
```

Отдельные проверки, которые стоит помнить:

```bash
.venv/bin/lint-imports              # границы доменов (3 контракта)
.venv/bin/python evals/run_golden.py  # golden-эвалы, офлайн, без сети
aegis doctor --json | jq .checks    # машинный вывод для HEALTHCHECK/мониторинга
aegis tools                         # реестр инструментов и их схем
```

## 5. Бэкапы и восстановление (пункт 1.11)

Правило: **бэкап без `verify` не считается бэкапом**.

```bash
make backup          # pg_dump -Fc → backups/aegis_<UTC>.dump (+ .sha256, ротация, rclone)
make backup-verify   # поднимает временную БД в том же кластере, restore-ит дамп, сверяет таблицы
make backup-list     # что есть локально
make restore FILE=backups/aegis_2026-09-04T031200Z.dump   # опасная операция, перезатирает рабочую БД
```

Переменные (`deploy/backup.sh`): `AEGIS_BACKUP_DIR`, `AEGIS_BACKUP_KEEP` (14),
`AEGIS_RCLONE_REMOTE` (например `crypt:aegis-backups` — зашифрованный remote rclone),
`AEGIS_PG_SERVICE|PG_DB|PG_USER`.

Установка таймера (systemd, `Persistent=true` догоняет пропущенный ночной запуск):

```bash
sudo cp deploy/systemd/aegis-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now aegis-backup.timer
systemctl list-timers aegis-backup.timer
journalctl -u aegis-backup.service -n 50
```

Cron-вариант (если systemd не ваш случай — тогда сам себе напоминалка о `verify`):

```cron
40 3 * * * cd /srv/aegis && ./deploy/backup.sh backup && ./deploy/backup.sh verify >> backup.log 2>&1
```

Что проверяет `verify`: что дамп читается (`pg_restore --list`), что после восстановления есть
схемы `platform/governance/memory/knowledge`, и что таблицы `events`, `llm_calls`, `tool_runs`,
`facts`, `notes` доступны и не пустые «в никуда». Любое расхождение → exit 1.

## 6. De-escalation и деградация

| Симптом | Что происходит | Что делать |
|---|---|---|
| LLM недоступен (429/5xx, все провайдеры) | ответ деградирует: «модель недоступна», команды и парсеры работают | `make logs`; проверить `GLM_API_KEY`, при необходимости включить `FALLBACK_*` |
| Postgres лёг | события/аудит не пишутся, память недоступна; бот отвечает детерминированно | проверить контейнер, `make migrate` не нужен; после поднятия всё пишется дальше |
| Redis лёг | **бот продолжает отвечать**: история хода теряется, подтверждения честно отказываются («память сессий недоступна»), бюджет считается локальным счётчиком процесса | `docker compose restart redis`; AOF вернёт ключи; в `/status` виден флаг `kv_degraded` |
| Исчерпан дневной бюджет | thinking выключается, затем только `fast`; после 100 % — отказ с текстом про `/cost` | `make psql` → `SELECT * FROM platform.v_cost_daily` ; поднять `DAILY_BUDGET_USD` |
| Нужна экстренная остановка записей | `/halt` (kill switch): write-действия запрещены, чтение живёт | `/resume` — снять |
| Подтверждение не нажато вовремя | `pending_actions` протухает по TTL, действие не выполнено | повторить запрос |

Команды бота: `/start`, `/help`, `/new` (сброс контекста), `/cost`, `/status`, `/tools`, `/halt` (заморозить записи, можно с причиной: `/halt отпуск`), `/resume`.

## 7. Прямые вопросы к данным

```bash
make psql   # psql -U aegis -d aegis
```

```sql
-- куда ушли деньги за сегодня
SELECT * FROM platform.v_cost_daily ORDER BY day DESC LIMIT 7;
-- что агент решал про мои действия
SELECT created_at, tool, decision, left(coalesce(result,''), 80)
  FROM governance.tool_runs ORDER BY id DESC LIMIT 20;
-- полный след одного ответа (воспроизводимость, принцип 4)
SELECT role, model, prompt_tokens, completion_tokens, cost_usd, ok, error
  FROM platform.llm_calls WHERE trace_id = '<trace_id из /status или ответа>';
-- память
SELECT category, fact, created_at FROM memory.facts WHERE valid_to IS NULL ORDER BY created_at DESC LIMIT 20;
```

SearXNG требует явного включения JSON-ответов: `deploy/searxng/settings.yml` →
`formats: [html, json]`. Признак проблемы — `aegis doctor` без `--quick` отдаёт
`searxng.ok=false` с подсказкой.

## 8. Обновление ценников моделей

Стоимость считается по `in_usd_per_m`/`out_usd_per_m` из `platform/gateway/models.py`; цены
провайдера меняются. Раз в месяц: сверить каталог с прайсом, поправить числа, `make test` (тесты
проверяют формулы, а не конкретные цены).

Цены лежат в `PRICES`, ключ — **имя модели**, потому что одно и то же у разных шлюзов стоит
по-разному: `glm-4.6` (z.ai, $0.6/$2.2) и `z-ai/glm-4.6` (ZenMux, ступень <32k ≈ $0.35/$1.54).
Имя переопределяется через `MODEL_*` в `.env`; если для нового имени цены в `PRICES` нет, берётся
прайс роли и в лог уходит `models.unknown_price` с подсказкой — молча врать про бюджет хуже, чем
попросить дописать строчку.

Сверять цены и id удобно у самого роутера: `GET https://zenmux.ai/api/v1/models` отдаёт
публичный список `id / input_modalities / context_length / pricings` — оттуда и взяты строки для
`z-ai/glm-5.3-flash` (текст+картинки+видео, 1M контекста, промо $0.075/$0.25 до 09.09.2026,
лист $0.15/$0.50). В каталоге держим **листовую** цену: недооценённый бюджет ломает SLO молча, а
переоценённый лишь раньше посадит thinking в режим экономии.

Отдельная ловушка 5.3-серии: `thinking.type: "disabled"` не поддерживается (только `enabled`).
Раньше деградация бюджета именно так и экономила токены — на 5.3 это стало бы 400 на каждом
сообщении. Поэтому у имён из `THINKING_ALWAYS_ON` мы никогда не просим выключить reasoning, а
экономим сменой роли на `fast`.

Смена провайдера целиком:

```ini
GLM_BASE_URL=https://zenmux.ai/api/v1/
MODEL_BRAIN=z-ai/glm-5.3-flash
MODEL_VISION=z-ai/glm-5.3-flash   # 5.3-flash нативно мультимодальный: одна модель на обе роли
MODEL_FAST=z-ai/glm-4.7-flashx
LLM_THINKING_PARAM=false   # только если роутер отвечает 400 на thinking
EMBED_BASE_URL=            # пусто = векторы через тот же роутер; при 404 — https://api.z.ai/api/paas/v4/
EMBED_API_KEY=             # и тогда же — ключ z.ai отдельно
```

Префикс `z-ai/` обязателен: без него роутер не знает модель. Проверка после правки —
`aegis doctor --models` (по одному запросу на роль + размерность эмбеддинга).

## 9. Локальная разработка без Docker

```bash
uv venv .venv --python python3.12 && source .venv/bin/activate
pip install -e ".[dev]"
make fmt lint type imports
pytest -q -m "not integration"      # интеграции требуют AEGIS_TEST_DATABASE_URL
python evals/run_golden.py
```

`pytest -m integration` поднимает схему через `alembic upgrade head` на тестовой БД; без
`AEGIS_TEST_DATABASE_URL` тесты скипаются — так и задумано, чтобы юниты оставались офлайн.

Docker для интеграций не обязателен: extra `dev` тянет `pgserver` (переносной Postgres 16 с
pgvector), и `make test-live` сам поднимает кластер в `.cache/aegis-pg`, накатывает миграции и
выполняет команду. Кластер переиспользуется, повторный прогон занимает секунды:

```bash
make test-live                       # portable-Postgres + миграции + pytest -m integration
python tools/with_local_pg.py --fresh --strip-ext -- pytest -q -m integration  # с нуля
```

Флаг `--strip-ext` вырезает из миграции trigram-индексы (в переносном кластере нет
`pg_trgm`/`pgcrypto`); на боевом образе `pgvector/pgvector:pg16` они накатываются целиком.

## 10. Диагностика «бот молчит»

1. `docker compose -f deploy/docker-compose.yml ps bot` — не в `restart`-цикле?
2. `logs bot | tail -50` — `owner_only.rejected` означает, что `TELEGRAM_OWNER_ID` не совпал.
3. Polling не работает с прокси/файрволом → в логах `TelegramServerError`.
4. Токен валиден? `curl -s "https://api.telegram.org/bot$TOKEN/getMe"`.
5. «Сбой: ...» на каждое сообщение, команды при этом живы → смотри §10.1.

### 10.1 Сбой на всех сообщениях при живых командах

Команды (`/start`, `/status`, `/cost`) не пишут в БД, а свободный текст пишет: событие в
`platform.events` и строку в `platform.llm_calls`. Значит «команды работают, текст падает» —
это почти всегда путь записи, а не модель.

```powershell
# трейс последнего сбоя (структурный лог пишется в stderr контейнера)
docker compose -f deploy/docker-compose.yml logs --tail 300 bot | Select-String "handle.failed" -Context 0,40
docker compose -f deploy/docker-compose.yml exec bot aegis doctor --quick
```

Смотреть на `checks.postgres`: `ok: false` + `hint: накай миграции` означает, что бот поднят без
`alembic upgrade head`. Лечится тем самым `make migrate`. С тех пор как `/status` показывает
`Трассировка: ⚠️ не пишется (N сбоев)`, состояние видно без логов: откры порт ≠ пишутся строки.

Тот же корень показывает и проверка схемы:

```powershell
docker compose -f deploy/docker-compose.yml exec postgres psql -U aegis -d aegis -c `
  "select count(*) from information_schema.tables where table_schema in ('platform','governance','memory','knowledge')"
```

`0` — таблицы не создавались вовсе. Тогда сначала `run --rm bot alembic upgrade head` (его вывод
и есть настоящий лог ошибки миграции — не подавляй его), и только потом перезапуск бота.

Исторически (до фиксации) в этом положении было `Сбой: TypeError`, потому что обработчик отказа
в `BestEffortEventSink` сам кидал исключение — логирует лишний ключ `event`, который structlog
резервирует под текст сообщения. Если снова видишь TypeError в `_degradation`/`sink`-коде — это
возвращение того же класса багов, регрессия закрыта в `tests/test_degradation.py`
(в том числе статическим запретом `log.*(event=...)` по всему `src`).

### 10.2 «Модели сейчас недоступны»

Сообщение теперь всегда с причиной — она вычисляется из transport-ошибки и маскируется, так что
в Telegram не попадают заголовки и ключи:

| Причина в скобках | Что делать |
| ----------------- | ---------- |
| `401 — ключ не принят` | `GLM_API_KEY` в `.env` не тот/отозван → обновить и `up -d --force-recreate bot` |
| `404 — неверный путь` | `GLM_BASE_URL` обязан быть `https://api.z.ai/api/paas/v4/` (OpenAI-клиент сам дописывает `/chat/completions`) |
| `429` | квота/частота: пауза, либо перевести роль на `glm-4.5-flash` |
| `соединение ... TLS` | антивирус/корпоративный прокси перехватывает сертификат; из контейнера — `docker compose exec bot python -c "import httpx;print(httpx.get('https://api.z.ai', timeout=10).status_code)"` |
| `таймаут` | поднять `LLM_TIMEOUT_S` или проверить VPN |
| `5xx` | живёт и проходит само; fallback-модель уже была перепробована |
| `провайдер не знает такую модель` | у роутеров имя с префиксом провайдера (`z-ai/glm-4.6` для ZenMux) — сверь `MODEL_*` и список роутера |
| `400` при живом ключе и верном имени | роутер не понимает нестандартный `thinking` → `LLM_THINKING_PARAM=false` |

Одна команда проверяет всё, что связано с «бот не думает»:

```powershell
docker compose -f deploy\docker-compose.yml exec bot aegis doctor
```

В отличие от `--quick` (его использует HEALTHCHECK, чтобы не тратить бюджет и не ходить в сеть)
полный `doctor` делает настоящий запрос к модели на 8 токенов и печатает `model: ok · glm-4.5-flash
· 742 мс · $0.000010` или причину с подсказкой.

Если ключ и сеть в порядке, а бот всё равно «не думает» — проверь имена моделей:

```powershell
docker compose -f deploy\docker-compose.yml exec bot aegis doctor --models
```

По одному запросу на роль (`brain`/`fast`/`vision`) + эмбеддинги; там же печатается `dims`, так
что расхождение размерности `embedding-3` с колонкой `vector(2048)` видно сразу. Список моделей у
z.ai меняется (для картинок устойчиво существует `glm-4.5v`, `glm-4.6v` появился позже), так что
переопределение в `.env` — `MODEL_BRAIN` / `MODEL_FAST` / `MODEL_VISION` / `MODEL_EMBED` — штатный
способ удержать конфиг рабочим без правки кода.

### 10.3 Неизвестная команда

`/restart`, `/update`, `/logs` бот не выполняет — и, чтобы не списывать токены на «понимание»
таких сообщений, отвечает сразу: список команд + где перезапускают контейнер. Проверка
по форме первой косой черты, поэтому `/home/user/что-то` в текст не вмешивается.
