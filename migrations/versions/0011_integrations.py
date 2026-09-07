"""0011: подключения — MCP-серверы, именованные API-ключи, локальные плагины.

Реестр, а не «файл настроек»: подключением управляет владелец из CLI и чата, и оно обязано
переживать рестарты, показывать состояние (последняя ошибка пробы) и попадать в аудит — как
всё остальное в системе.

Секрет (API-ключ, токен MCP-сервера) хранится ЗАВЁРНУТЫМ BLOB-ШИФРОМ (тот же BlobCipher, что
у журнала): в базе — только шифртекст + wrapped DEK + версия KEK. Колонка `secret_env` говорит,
как ключ подставляется (для MCP — имя переменной окружения процесса-сервера), а сам ключ в
env-конфиге строкой не лежит никогда: иначе он утёк бы в `docker inspect`.

kind закрыт CHECK'ом: 'mcp' (stdio-процесс), 'api' (именованный секрет для будущих сервисов и
плагинов), 'plugin' (модуль-обёртка из venv). Всё, что не перечислено, — не «подключение».
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        SET LOCAL statement_timeout = '30s';
        CREATE SCHEMA IF NOT EXISTS integrations;
        CREATE TABLE integrations.connectors (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id bigint NOT NULL,
            kind text NOT NULL CHECK (kind IN ('mcp', 'api', 'plugin')),
            name text NOT NULL CHECK (length(name) BETWEEN 2 AND 64),
            enabled boolean NOT NULL DEFAULT true,
            config jsonb NOT NULL DEFAULT '{}'::jsonb,
            secret_ct bytea,
            secret_wrapped bytea,
            secret_key_version integer,
            last_error text,
            last_ok_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT connectors_name_per_owner UNIQUE (owner_id, kind, name),
            CONSTRAINT connectors_secret_shape CHECK (
                (secret_ct IS NULL AND secret_wrapped IS NULL)
                OR (secret_ct IS NOT NULL AND secret_wrapped IS NOT NULL)
            )
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        DROP TABLE IF EXISTS integrations.connectors;
        DROP SCHEMA IF EXISTS integrations;
        """
    )
