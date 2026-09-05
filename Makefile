PY ?= python3
# --env-file нужен для подстановки ${SEARXNG_SECRET} и т.п. из корневых .env
ENVFILE = $(if $(wildcard .env),--env-file .env,)
COMPOSE = docker compose $(ENVFILE) -f deploy/docker-compose.yml

.PHONY: help check evals up test-live up-durable down restart logs ps migrate test test-all lint fmt type imports doctor backup backup-verify backup-list restore clean

help: ## что умеет Makefile
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[1m%-10s\033[0m %s\n", $$1, $$2}'

up: ## поднять стек (pg+redis+nats+searxng+temporal+bot)
	$(COMPOSE) up -d --build

up-core: ## только ядро шага 1: postgres, redis, searxng, bot
	$(COMPOSE) up -d --build postgres redis searxng bot

up-durable: ## ядро + NATS/Temporal (профиль durable)
	$(COMPOSE) --profile durable up -d --build

down: ## остановить стек
	$(COMPOSE) down

restart: ## перезапустить бота
	$(COMPOSE) restart bot

logs: ## логи бота
	$(COMPOSE) logs -f bot

ps: ## статус контейнеров
	$(COMPOSE) ps

migrate: ## alembic upgrade head внутри контейнера бота
	$(COMPOSE) run --rm bot alembic upgrade head

psql: ## интерактивный psql в контейнере postgres
	$(COMPOSE) exec postgres psql -U aegis -d aegis

doctor: ## локальная самодиагностика конфигурации и связностей
	$(PY) -m aegis.cli doctor

test: ## только юнит-тесты (без БД)
	pytest -q

test-live: ## интеграции на переносном Postgres (без Docker; база каждый раз чистая)
	$(PY) tools/with_local_pg.py --strip-ext --fresh -- pytest -q -m integration

test-all: ## юниты + интеграции: есть AEGIS_TEST_DATABASE_URL/докер — он, иначе portable-PG
	@if [ -n "$$AEGIS_TEST_DATABASE_URL" ]; then \
		AEGIS_TEST_DATABASE_URL=$$AEGIS_TEST_DATABASE_URL pytest -q; \
	elif $(PY) -c "import pgserver" >/dev/null 2>&1; then \
		$(MAKE) test-live; \
	else \
		echo "нужен Postgres: подними стек (make up-core) или поставь extra dev (pgserver)"; \
		AEGIS_TEST_DATABASE_URL=$${AEGIS_TEST_DATABASE_URL:-postgresql+asyncpg://aegis:aegis@localhost:5432/aegis} pytest -q; \
	fi

fmt: ## автоформат + автофиксы
	ruff format src tests migrations evals tools
	ruff check --fix src tests migrations evals tools

lint: ## все статические проверки
	ruff check src tests migrations evals tools
	$(MAKE) type
	$(MAKE) imports

check: ## полный офлайн-контур качества (то, что требует CI)
	$(MAKE) lint
	$(MAKE) test
	$(MAKE) evals

evals: ## золотые наборы (роутинг/политика/DLP/рендер) + хэши журнала, без сети
	python evals/run_golden.py
	python evals/run_repro.py

type:
	mypy src

imports: ## проверка границ доменов
	lint-imports

backup: ## pg_dump + проверка читаемости + ротация (+rclone, если задан AEGIS_RCLONE_REMOTE)
	bash ./deploy/backup.sh backup

backup-verify: ## контрольное восстановление последнего дампа во временную БД (делать сразу после backup)
	bash ./deploy/backup.sh verify

backup-list: ## список локальных дампов
	bash ./deploy/backup.sh list

restore: ## восстановление рабочей БД: make restore FILE=backups/aegis_....dump
	@test -n "$(FILE)" || (echo "usage: make restore FILE=backups/aegis_<ts>.dump"; exit 2)
	bash ./deploy/backup.sh restore $(FILE)

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
