"""Парсер-наблюдатель (schema parsing) + состояние динамического шифрования (platform.vault_state).

Парсер — «руки, которые читают всё»: источники трёх семейств (страница/RSS-лента/публичный
Telegram-канал), предметная дедупликация отпечатком, фильтры-иголки, тело — в шифровании
(body_sealed), заголовок и выдержка открыты: без них невозможны ни индекс, ни превью, а в них
секрета владельца нет (источники публичные по определению).

vault_state — ОДНА строка-счётчик поколений KEK-цепи. Не хранилище ключей: ключи выводятся из
CRYPTO_KEK (env) и в базу не попадают никогда; здесь живёт только «какое поколение считать
активным», чтобы все процессы (бот, CLI, демон, мини-апп) писали согласно одному числу.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        SET LOCAL statement_timeout = '30s';
        CREATE SCHEMA IF NOT EXISTS parsing;
        CREATE TABLE parsing.sources (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id bigint NOT NULL,
            kind text NOT NULL DEFAULT 'auto'
                CHECK (kind IN ('auto', 'web', 'rss', 'atom', 'jsonfeed', 'tg_channel')),
            target text NOT NULL CHECK (length(target) BETWEEN 4 AND 1024),
            label text NOT NULL DEFAULT '',
            interval_sec int NOT NULL DEFAULT 900 CHECK (interval_sec >= 30),
            include_kw text,
            exclude_kw text,
            enabled boolean NOT NULL DEFAULT true,
            cursor jsonb NOT NULL DEFAULT '{}'::jsonb,
            last_check timestamptz,
            last_ok timestamptz,
            last_error text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (owner_id, kind, target)
        );
        CREATE TABLE parsing.items (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            source_id uuid NOT NULL REFERENCES parsing.sources(id) ON DELETE CASCADE,
            owner_id bigint NOT NULL,
            fingerprint text NOT NULL CHECK (length(fingerprint) = 64),
            url text NOT NULL DEFAULT '',
            title text NOT NULL DEFAULT '',
            author text NOT NULL DEFAULT '',
            excerpt text NOT NULL DEFAULT '',
            body text NOT NULL DEFAULT '',
            body_sealed boolean NOT NULL DEFAULT false,
            media jsonb NOT NULL DEFAULT '[]'::jsonb,
            published_at timestamptz,
            status text NOT NULL DEFAULT 'new'
                CHECK (status IN ('new', 'pushed', 'read', 'archived')),
            first_seen timestamptz NOT NULL DEFAULT now(),
            UNIQUE (source_id, fingerprint)
        );
        CREATE INDEX parsing_items_new ON parsing.items (owner_id, first_seen DESC)
            WHERE status = 'new';
        CREATE INDEX parsing_items_source_seen ON parsing.items (source_id, first_seen DESC);
        CREATE INDEX parsing_sources_due ON parsing.sources (last_check NULLS FIRST)
            WHERE enabled;
        CREATE TABLE platform.vault_state (
            id smallint PRIMARY KEY CHECK (id = 1),
            gen bigint NOT NULL DEFAULT 0,
            prev_gen bigint,
            algo text NOT NULL DEFAULT 'hkdf-sha256',
            rotated_at timestamptz NOT NULL DEFAULT now(),
            note text NOT NULL DEFAULT ''
        );
        INSERT INTO platform.vault_state (id, gen, note) VALUES (1, 0, 'посеяно миграцией')
            ON CONFLICT (id) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        DROP TABLE IF EXISTS platform.vault_state;
        DROP SCHEMA IF EXISTS parsing CASCADE;
        """
    )
