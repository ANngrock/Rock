"""Хаб автоматизации: задачи (planning.tasks), крон-прогоны (planning.job), действия и
вебхуки (schema automation, миграция 0016).

Логика одна: «бот не только отвечает — бот делает». Задачи — состояние воли владельца
(что сделано, что горит); крон-прогон — агент по расписанию сам выполняет промпт и
доставляет результат; эндпоинт — исходящий HTTP-вызов с секретными заголовками,
которые лежат запечатанными ключевой цепью (0015) и никогда не печатаются в журнал;
вебхук — входящий триггер: чужой мир может пнуть бота, но только через секрет и политику
владельца. Журнал runs — аудит: чем, когда, с каким ответом; секреты в digest замаскированы.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        SET LOCAL statement_timeout = '30s';

        CREATE TABLE planning.tasks (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id bigint NOT NULL,
            title text NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
            notes text NOT NULL DEFAULT '',
            tags text NOT NULL DEFAULT '',
            status text NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'doing', 'done', 'archived')),
            priority smallint NOT NULL DEFAULT 0 CHECK (priority BETWEEN -2 AND 2),
            due_at timestamptz,
            remind_on_due boolean NOT NULL DEFAULT false,
            done_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX planning_tasks_open
            ON planning.tasks (owner_id, due_at NULLS LAST, priority DESC)
            WHERE status IN ('open', 'doing');
        CREATE INDEX planning_tasks_remind_due
            ON planning.tasks (due_at)
            WHERE status = 'open' AND remind_on_due AND due_at IS NOT NULL;

        CREATE TABLE planning.job (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id bigint NOT NULL,
            title text NOT NULL CHECK (length(title) BETWEEN 2 AND 80),
            prompt text NOT NULL CHECK (length(prompt) BETWEEN 3 AND 4000),
            repeat text NOT NULL DEFAULT 'daily'
                CHECK (repeat IN ('once', 'every_min', 'hourly', 'daily', 'weekdays', 'weekly')),
            at_time text NOT NULL DEFAULT '',
            every_min int NOT NULL DEFAULT 30 CHECK (every_min BETWEEN 1 AND 1440),
            dow smallint NOT NULL DEFAULT 1 CHECK (dow BETWEEN 1 AND 7),
            channel text NOT NULL DEFAULT 'message' CHECK (channel IN ('message', 'silent')),
            status text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'paused', 'done')),
            next_run timestamptz NOT NULL,
            last_run timestamptz,
            last_error text NOT NULL DEFAULT '',
            fail_count smallint NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (owner_id, title)
        );
        CREATE INDEX planning_job_due ON planning.job (next_run) WHERE status = 'active';

        CREATE SCHEMA automation;
        CREATE TABLE automation.endpoint (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id bigint NOT NULL,
            name text NOT NULL CHECK (length(name) BETWEEN 2 AND 60),
            method text NOT NULL DEFAULT 'POST'
                CHECK (method IN ('GET', 'POST', 'PUT', 'PATCH', 'DELETE')),
            url text NOT NULL CHECK (url ~ '^https?://'),
            headers text NOT NULL DEFAULT '{}',
            body_template text NOT NULL DEFAULT '',
            secrets text NOT NULL DEFAULT '',
            secrets_sealed boolean NOT NULL DEFAULT false,
            timeout_ms int NOT NULL DEFAULT 15000 CHECK (timeout_ms BETWEEN 500 AND 120000),
            enabled boolean NOT NULL DEFAULT true,
            last_run timestamptz,
            last_status text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (owner_id, name)
        );
        CREATE TABLE automation.run (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            endpoint_id uuid NOT NULL REFERENCES automation.endpoint(id) ON DELETE CASCADE,
            owner_id bigint NOT NULL,
            ok boolean NOT NULL,
            status int NOT NULL DEFAULT 0,
            ms int NOT NULL DEFAULT 0,
            digest text NOT NULL DEFAULT '',
            triggered_by text NOT NULL DEFAULT 'model'
                CHECK (triggered_by IN ('model', 'menu', 'cli', 'job'))
        );
        CREATE INDEX automation_run_recent
            ON automation.run (owner_id, id DESC);

        CREATE TABLE automation.hook (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id bigint NOT NULL,
            name text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_-]{2,40}$'),
            secret text NOT NULL,
            secret_sealed boolean NOT NULL DEFAULT false,
            policy text NOT NULL DEFAULT 'notify' CHECK (policy IN ('notify', 'turn')),
            rate_per_min int NOT NULL DEFAULT 6 CHECK (rate_per_min BETWEEN 0 AND 120),
            enabled boolean NOT NULL DEFAULT true,
            fires int NOT NULL DEFAULT 0,
            last_fire timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (name)
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        DROP SCHEMA IF EXISTS automation CASCADE;
        DROP TABLE IF EXISTS planning.job;
        DROP TABLE IF EXISTS planning.tasks;
        """
    )
