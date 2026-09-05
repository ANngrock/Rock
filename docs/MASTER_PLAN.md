# MASTER_PLAN — aegis

Документ-контракт: то, чем я руководствуюсь на каждом шаге. Изменения — только через PR к этому
файлу (иначе «план» превращается в пересказ того, что уже сделано).

## 1. Идентичность проекта

| | |
|---|---|
| Название | **aegis** — личный AI-менеджер |
| Пользователь | один (owner). Telegram — основной интерфейс |
| Мозг | GLM-4.6 (текст, tool calling, thinking on/off) |
| Зрение | GLM-4.6V / 4.5V — как инструмент, не как режим работы |
| Фон | GLM-4.5-Air / Flash; позже — локальные дистиллированные модели |
| Способности | искать и читать в интернете, принимать файлы/ссылки, рассказывать, записывать (траты, задачи, заметки, факты), напоминать, смотреть на фото/видео/камеру, подключать сервисы |
| Уровень | enterprise-принципы в модульном монолите: event sourcing, durable workflows, policy engine, observability, evals, DR |

Не является: публичным ассистентом, мульти-тенант SaaS, «промптом с ботом».

## 2. Незыблемые принципы (проверяются в каждом шаге)

1. **Данные живут в БД, LLM — слой понимания.** Никакого состояния «в контексте»: история — кэш,
   факты/заметки/журналы — в Postgres, состояние процессов — в Temporal.
2. **Любое write-действие проходит через policy engine** → `allow | confirm | deny`. Модель не
   может это обойти, потому что решение принимает код между «получен tool_call» и «вызван handler».
3. **Внешний контент — untrusted** (web, чеки, файлы, распознавание). Секреты не попадают в
   промпт; PII маскируется DLP-слоем; инструкции из данных не исполняются (правило 2 промпта +
   карантин dual-LLM на шаге 2).
4. **Всё воспроизводимо**: версия промпта, модель, токены, стоимость, вызовы инструментов, решения
   политики — в трассе (event store + `platform.llm_calls` + `governance.tool_runs`).
5. **Деградация без LLM**: команды, текстовый поиск, policy и детерминированные парсеры работают
   всегда; при отказе модели бот отвечает честно, а не падает.
6. **Каждый шаг заканчивается тем, чем я реально пользуюсь ежедневно.** Фича без ежедневного
   применения переносится в конец очереди.
7. **Границы доменов жёсткие** (import-linter), инструменты агентов — тонкие обёртки над use cases
   доменов, не наоборот.

## 3. Bounded contexts и структура репозитория

```
aegis/
├── src/aegis/
│   ├── platform/        # config, db, kv, events(store+outbox), gateway(LLM/DLP/cost), logging
│   ├── governance/      # policy engine, scopes, audit, kill switch
│   ├── agents/          # supervisor, specialists, tools registry, prompts, verifier
│   ├── memory/          # facts, episodes, knowledge graph, consolidation
│   ├── knowledge/       # notes, documents, web pages, RAG
│   ├── finance/         # ledger, accounts, budgets, forecasts      (шаг 3)
│   ├── planning/        # tasks, calendar, CP-SAT                   (шаг 5)
│   ├── web/             # search, fetch, browser agent, watchers
│   ├── proactivity/     # user model, attention budget              (шаг 8)
│   └── interaction/     # telegram bot, mini app api, renderer
├── migrations/          # alembic (Postgres-схемы по доменам)
├── tests/               # unit / property / integration
├── evals/               # golden datasets + офлайн-раннер
├── deploy/              # docker-compose (шаг 1), k3s (позже)
├── docs/                # MASTER_PLAN.md, ADR/, EXECUTION_LOG.md, RUNBOOK.md
└── .github/workflows/ci.yml
```

Правило зависимостей: `interaction → agents → (domains | platform)`; `domains → platform`;
`governance → platform`. Платформа не знает о доменах, домены — друг о друге, governance — ни о
доменах, ни об агентах. Проверка: `make imports`.

## 4. Целевые NFR (SLO)

| Метрика | Цель | Как измеряется |
|---|---|---|
| Доступность | 99.9 % | uptime-пробы `aegis doctor` + `restart: unless-stopped`; деградация считается доступной |
| Латентность простых запросов | p50 < 1.5 с, p95 < 4 с | `platform.llm_calls.latency_ms` |
| RPO / RTO | 0 для finance (WAL-репликация) / < 15 мин | `archive_mode=on`, pg_dump + offsite |
| Финансовые инварианты | сумма проводок = 0; баланс = свёртка журнала | property-тесты (шаг 3) |
| Стоимость | ≤ бюджет/день, деградация плана при приближении | `CostGovernor` (уровни 0/1/2) |
| Приватность | PII не покидает периметр без маскирования | DLP + тесты на «маска не содержит цифр» |
| Воспроизводимость | любой ответ разворачивается из трассы | event store + llm_calls + tool_runs по `trace_id` |

## 5. Дорожная карта

| Шаг | Содержание | Статус |
|---|---|---|
| 1. Фундамент | репозиторий, Docker, Postgres+pgvector, Redis, NATS, Temporal, SearXNG; config; event store+outbox; Model Gateway (GLM + fallback + retry + cost + DLP + запись вызовов); tool registry; policy engine; supervisor v1 с tier-роутингом и подтверждениями; Telegram bot с whitelist; память фактов, заметки, web search/fetch, vision-инструмент; миграции; тесты; CI | **сделан, ожидает запуска у владельца** |
| 2. Ядро агента 2.0 | Temporal workflow сессии, Verifier, dual-LLM для untrusted, стриминг, STT (faster-whisper), напоминания (scheduler), Langfuse, golden datasets v1 (live-режим) | **Verifier + dual-LLM карантин, напоминания и стриминг ответов сделаны** (ADR-0009, ADR-0010, ADR-0011); Temporal/NATS/Langfuse/STT и golden datasets v2 — ждут |
| 3. Finance | ledger двойной записи, счета/валюты, ввод текст/голос, категории+правила, бюджеты, семантический слой метрик, дайджесты, property-тесты | ожидает |
| 4. Vision 2.0 | чеки/QR ФНС, документы, скриншоты→задачи, ingest в Knowledge, карантинная модель | ожидает |
| 5. Planning | задачи/календарь, CP-SAT план дня, брифинги, привычки | ожидает |
| 6. Память 2.0 | эпизоды, временной граф, консолидация, GraphRAG, /forget | ожидает |
| 7. Web-агент | Playwright в песочнице, GUI-агент на GLM-V, watchers, самопочинка | ожидает |
| 8. Проактивность | user model, ситуационный поток, attention budget | ожидает |
| 9. Mini App + Realtime | дашборды, LiveKit, голос с barge-in, зрение в потоке, адаптер GLM-Realtime | ожидает |
| 10. Самообучение | дистилляция, DSPy-контур, Toolsmith с PR | ожидает |
| 11. Платформа | Skill SDK, MCP-сервер, HA, chaos, DR-учения | постоянно |

## 6. Ключевые решения (ADR)

| ADR | Решение |
|---|---|
| [0001](ADR/0001-language-and-frameworks.md) | Python 3.12 (минимум 3.11), aiogram 3, SQLAlchemy 2 async, Pydantic v2; FastAPI — на шаге 9 |
| [0002](ADR/0002-glm-via-openai-compatible-sdk.md) | GLM через OpenAI-совместимый SDK; провайдер абстрагирован в ModelGateway |
| [0003](ADR/0003-modular-monolith.md) | модульный монолит + Postgres-схемы по доменам; выделение сервисов — только по необходимости |
| [0004](ADR/0004-event-store-and-outbox.md) | event store в Postgres + transactional outbox → NATS JetStream |
| [0005](ADR/0005-temporal-for-long-running.md) | Temporal для всего, что живёт дольше одного запроса |
| [0006](ADR/0006-confirmations.md) | подтверждения через pending_actions (Redis, TTL) + inline-кнопки; решает policy engine |
| [0007](ADR/0007-web-stack.md) | self-hosted SearXNG + trafilatura; встроенный поиск GLM — резерв; SSRF-фильтр обязателен |
| [0008](ADR/0008-decision-journal.md) | журнал решений в Postgres: контент-адресные блобы, хэш-цепочка, якорь дня; replay с замороженным миром |
| [0009](ADR/0009-answer-verification-and-quarantine.md) | ответ сверяется с источниками (числа — кодом, смысл — судом в чистом контексте); внешний текст размечает отдельная роль, планировщик его не видит |
| [0010](ADR/0010-reminders-on-a-table-and-a-timer.md) | напоминания: момент считает парсер, а не модель; расписание живёт в `planning.reminders`, исполняется тиком systemd-таймера с арендой строки — временная замена Temporal с теми же обещаниями |
| [0011](ADR/0011-streaming-replies.md) | стриминг — транспорт, а не второй путь: `chat_stream` отдаёт тот же `ChatResult`; обрыв после первого куска = неполный ответ, а не повторный запрос; в Telegram итог дописывается в то же сообщение |

## 7. Что явно не делаем

- Мульти-тенантность, авторизацию «на несколько владельцев», веб-админку «для красоты».
- Свой UI до шага 9: Telegram покрывает 90 % сценариев дешевле.
- Хранение состояния агента в памяти процесса (ломает и деградацию, и durable-workflow).
- «Умные» промпты вместо кода там, где достаточно парсера/политики.
- Автоматические внешние действия (покупки, звонки, переписка от моего имени) без явного
  подтверждения — пока не появится Verifier + карантин untrusted (шаг 2).
