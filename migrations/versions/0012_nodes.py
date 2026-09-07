"""0012: узлы — компьютеры владельца, управляемые из Telegram.

Модель одна с очередью ходов: управление машиной вне сервера — это всегда «доставка с
неизвестной задержкой», и единственный честный способ — строка-намерение с состояниями, а не
«сообщил в NATS и забыл». NATS здесь только труба: факт живёт в БД, переживает рестарты обоих
концов и читается в аудите.

Узел подключается ИСХОДЯЩИМ соединением (демон на ПК тянет команды по подписке, толкает
результаты публикацией): входящих портов на машине владельца не появляется вовсе — ноутбук за
NAT не становится дырой. Связка — одноразовый цифровой код: `aegis node enroll` выдал, владелец
ввёл демону, hash(код) сравнялся — paired. Код в БД не лежит (только sha256), TTL 15 минут.

payload команд — jsonb свободной формы НЕ бывает: схему держит код (planning/nodes.py), БД —
ограничения состояний и «исполнена/не исполнена»: результат run'а может содержать чужие
данные, он печатается владельцу и в лог не уходит целиком.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        SET LOCAL statement_timeout = '30s';
        CREATE TABLE planning.nodes (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id bigint NOT NULL,
            name text NOT NULL CHECK (length(name) BETWEEN 2 AND 64),
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'paired', 'revoked')),
            pairing_hash text,
            pair_expires_at timestamptz,
            paired_at timestamptz,
            last_seen timestamptz,
            caps jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT nodes_name_per_owner UNIQUE (owner_id, name)
        );
        CREATE TABLE planning.node_commands (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            node_id uuid NOT NULL REFERENCES planning.nodes(id) ON DELETE CASCADE,
            owner_id bigint NOT NULL,
            action text NOT NULL
                CHECK (action IN ('system', 'notify', 'screenshot', 'run')),
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            status text NOT NULL DEFAULT 'queued'
                CHECK (status IN
                    ('queued', 'dispatched', 'done', 'failed', 'expired', 'cancelled')),
            result text,
            error text,
            trace_id text,
            created_at timestamptz NOT NULL DEFAULT now(),
            dispatched_at timestamptz,
            done_at timestamptz,
            expires_at timestamptz NOT NULL
        );
        CREATE INDEX node_commands_open
            ON planning.node_commands (node_id, created_at)
            WHERE status IN ('queued', 'dispatched');
        """
    )


def downgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        DROP TABLE IF EXISTS planning.node_commands;
        DROP TABLE IF EXISTS planning.nodes;
        """
    )
