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
| `GLM_API_KEY` | — | ключ z.ai (OpenAI-совместимый API) |
| `GLM_BASE_URL` | `https://api.z.ai/api/paas/v4/` | основной провайдер |
| `MODEL_BRAIN` / `MODEL_VISION` / `MODEL_FAST` / `MODEL_EMBED` | каталог | переопределение имён моделей без правки кода |
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

Стоимость считается по `in_usd_per_m`/`out_usd_per_m` из `platform/gateway/models.py`; цены z.ai
меняются. Раз в месяц: сверить каталог с прайсом, поправить числа, `make test` (тесты проверяют
формулы, а не конкретные цены). Имена моделей — переопределить через `MODEL_*` в `.env`, если не
хочется трогать код.

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

Исторически (до фиксации) в этом положении было `Сбой: TypeError`, потому что обработчик отказа
в `BestEffortEventSink` сам кидал исключение — логирует лишний ключ `event`, который structlog
резервирует под текст сообщения. Если снова видишь TypeError в `_degradation`/`sink`-коде — это
возвращение того же класса багов, регрессия закрыта в `tests/test_degradation.py`
(в том числе статическим запретом `log.*(event=...)` по всему `src`).
