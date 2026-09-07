# aegis

Личный AI-менеджер: один пользователь, Telegram как основной интерфейс, GLM как мозг.
Текущее состояние — **Шаг 1 «Фундамент»** (каркас, которым уже можно пользоваться ежедневно).

Не «обёртка над ChatGPT»: данные живут в БД, каждое действие проходит через policy engine,
каждый вызов модели и инструмента попадает в трассу. LLM — слой понимания, а не хранилище.

---

## Что уже работает (шаг 1)

| Возможность | Как выглядит для владельца |
|---|---|
| Поиск в интернете | «найди 3 ссылки про pgvector hnsw» → структурированный ответ со ссылками |
| Чтение страниц | «сохрани https://… в заметки» → страница извлечена, лежит в базе, ищется |
| Заметки | «запиши идею: …» → `knowledge.notes`, поиск по смыслу + тексту |
| Долговременная память | «запомни, что я не пью кофе после 16» → собирается в системный промпт |
| Фото | прислать изображение + вопрос → `analyze_image` (GLM-V), извлечение текстов/цифр |
| Подтверждения | запись среднего/высокого риска → inline-кнопки «Выполнить / Отмена» |
| Экономика | `$`-бюджет на день, автоматическая деградация thinking → fast-модель |
| Приватность | карта/телефон/почта/паспорт маскируются до отправки провайдеру |
| Аварийная остановка | `/halt` — записи запрещены, чтение и команды продолжают работать |

Команды: `/help` `/new` `/cost` `/status` `/tools` `/halt` `/resume`.

## Быстрый старт (10 минут)

```bash
cp .env.example .env                 # заполнить: токен бота, owner_id, GLM_API_KEY
make up-core                         # postgres(+pgvector), redis, searxng, bot
make migrate                         # alembic upgrade head
docker compose -f deploy/docker-compose.yml exec bot alembic upgrade head  # если bot ещё не поднялся
make doctor                          # конфиг + связности (без секретов в выводе)
```

Нужно: [@BotFather](https://t.me/BotFather) → токен, [@userinfobot](https://t.me/userinfobot) → ваш
`user_id`, ключ `GLM_API_KEY` с [z.ai](https://z.ai). Подробно — [docs/RUNBOOK.md](docs/RUNBOOK.md).

Без Telegram можно проверить ядро прямо сейчас:

```bash
pip install -e ".[dev]"
aegis tools                          # список инструментов и их схем
aegis ask "какой сегодня день недели?"
aegis doctor --quick                 # диагностика конфигурации
make test                            # юнит-тесты (не требуют ни БД, ни сети)
```

## Архитектура

```
src/aegis/
├── platform/        config · db · kv · events(store+outbox) · gateway(LLM,DLP,cost) · logging
├── governance/      policy engine · kill switch · audit
├── agents/          supervisor · tools(registry+builtin) · prompts
├── memory/          факты о владельце
├── knowledge/       заметки/страницы + гибрид-поиск
├── web/             SearXNG-поиск · fetch + SSRF-фильтр
├── finance/ planning/ proactivity/     — границы доменов (шаги 3/5/8)
└── interaction/     telegram: bot + render
```

Зависимости направляют строго в одну сторону: `interaction → agents → (domains | platform)`,
домены друг друга не видят. Это не соглашение, а проверка: `make imports` (import-linter) падает
на любом нарушении.

## Шесть правил, по которым проверяется каждый шаг

1. Данные живут в БД, LLM — слой понимания; состояние «в контексте» запрещено.
2. Любой write идёт через policy engine → `allow | confirm | deny`.
3. Внешний контент (web, файлы, распознавание) = untrusted; секреты не попадают в промпт, PII
   маскируется DLP-слоем.
4. Всё воспроизводимо: версия промпта, модель, токены, стоимость, вызовы инструментов — в трассе.
5. Деградация без LLM: команды, поиск по тексту, Policy и детерминированные парсеры работают всегда.
6. Шаг заканчивается тем, чем реально пользуются ежедневно, — иначе он не завершён.

Дорожная карта и статус: [docs/EXECUTION_LOG.md](docs/EXECUTION_LOG.md). Ключевые решения:
[docs/ADR/](docs/ADR). Полный план: [docs/MASTER_PLAN.md](docs/MASTER_PLAN.md).

## Операционный контур (шаг 2.5+)

Девять команд для того, что решается данными, а не рестартом — все работают и с живой БД, и
честно говорят «база не отвечает» (код возврата 1), и все покрыты интеграционным прогоном
(`tests/integration/test_cli_ops.py`):

```bash
aegis principals list|kind|grant|revoke|kill|budget   # права, бюджеты, kill-switch по принципалам
aegis flags list|set KEY --percent N|stale            # перцентили, allow/deny, контроль протухших
aegis policy lint|shadow [--limit]|golden            # правила как код: lock-сверка и теневой прогон
aegis retention plan|hold|forget|shreds|keyring       # dry-run→plan→apply; legal-hold; crypto-shredding
aegis events dlq|replay FROM_SEQ [--type T]           # разбор dead-letter и переигровка окна
aegis migrate status                                  # head vs БД, висячие бэкфиллы, dangling NOT NULL
aegis backfill status|run --rounds N|pause NAME       # бэкфиллы с лизингом, прогрессом и паузой
aegis slo status|alerts --write F|tick [--dry-run]    # burn-rate: состояние, артефакт алертов, тик
aegis turns status|release TRACE|drain OWNER [--execute]  # аренда ходов и залипшая очередь (F1)
aegis remind add --channel call … | remind test           # напоминание звонком; без провайдера — честный деград (шаг 2.9, RUNBOOK §21)
```

## Качество

`make check` = ruff + mypy --strict + import-linter + юниты + golden-evals (то же, что требует CI).
`make test` — только юниты (без сервисов), `make test-all` — с интеграцией против поднятого стека.
CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) дополнительно гоняет интеграцию с
pgvector/Redis и сборку образа ([evals/run_golden.py](evals/run_golden.py) — офлайн-эвалы).

## Бэкапы (пункт 1.11 — до шага 2)

```bash
make backup          # pg_dump -Fc + проверка читаемости + ротация (+rclone, если задан AEGIS_RCLONE_REMOTE)
make backup-verify   # контрольный restore во временную БД — без этого бэкап не считается бэкапом
```

Таймер — `deploy/systemd/aegis-backup.timer`; восстановление и разбор сбоев — `docs/RUNBOOK.md`.
