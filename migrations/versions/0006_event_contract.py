"""0006: контракт событий (F4) в схеме — owner-ключ, версия контракта, DLQ и «снято с ротации».

Разрезан с 0005 сознательно: политики RLS нельзя создавать на колонку, которой ещё нет, а
0005 — «слой параллельности и прав»; релиз с ними уже накатывался бы отдельно (F10: expand
шагами). Здесь ровно то, что просит слой событий: ``owner_id``/``schema_version`` на строке
события, abandonment-колонки в outbox (после DLQ строка перестаёт крутиться в попытках, но
не исчезает) и таблица ``event_dlq`` с полным телом отбракованного события.

``UNIQUE (event_id)`` в outbox — дубликат доставки из домена становится конфликтом вставки,
то есть репликой транзакции, а не «вторым фактом в мире» (ADR-0013).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        """
    )
    op.execute(
        """
        -- версия контракта фиксируется при рождении события: «на какой схеме ушло» читается
        -- из строки, а не выводится из сегодняшнего реестра (реестр успеет обновиться)
        ALTER TABLE platform.events
            ADD COLUMN IF NOT EXISTS owner_id bigint NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS schema_version integer NOT NULL DEFAULT 1;
        CREATE INDEX IF NOT EXISTS events_owner_time_idx
            ON platform.events (owner_id, occurred_at);
        CREATE INDEX IF NOT EXISTS events_schema_version_idx
            ON platform.events (event_type, schema_version);
        """
    )
    op.execute(
        """
        ALTER TABLE platform.outbox
            ADD COLUMN IF NOT EXISTS abandoned_at timestamptz,
            ADD COLUMN IF NOT EXISTS abandoned_reason text;
        -- «событие в очереди ровно один раз»: второй INSERT того же event_id — конфликт
        -- транзакции домена, а не вторая доставка в мир
        CREATE UNIQUE INDEX IF NOT EXISTS outbox_event_unique
            ON platform.outbox (event_id);
        -- выборка relay'я не должна сканировать весь outbox в поисках живых строк
        CREATE INDEX IF NOT EXISTS outbox_pending_idx
            ON platform.outbox (id) WHERE published_at IS NULL AND abandoned_at IS NULL;
        """
    )
    op.execute(
        """
        -- DLQ — не «чёрная дыра», а почтовый ящик с телом: причина, полный конверт, отметка
        -- переигрывания. replay_from_seq помечает replayed_at, но тело остаётся: разбор
        -- инцидента должен иметь что читать и ПОСЛЕ успешной доводки
        CREATE TABLE IF NOT EXISTS platform.event_dlq (
            id          bigserial   PRIMARY KEY,
            outbox_id   bigint      UNIQUE,
            event_id    text        NOT NULL DEFAULT '',
            reason      text        NOT NULL DEFAULT '',
            body        jsonb       NOT NULL DEFAULT '{}'::jsonb,
            dead_at     timestamptz NOT NULL DEFAULT now(),
            replayed_at timestamptz
        );
        CREATE INDEX IF NOT EXISTS event_dlq_dead_idx ON platform.event_dlq (dead_at DESC);
        """
    )
    op.execute(
        """
        ALTER TABLE platform.events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE platform.events FORCE ROW LEVEL SECURITY;
        CREATE POLICY events_household ON platform.events
            FOR SELECT USING (
                (coalesce(nullif(current_setting('app.principal_kind', true), ''),
                          'owner')) = 'service'
                OR (
                    (coalesce(nullif(current_setting('app.principal_kind', true), ''),
                              'owner')) = 'owner'
                    AND owner_id IN (0,
                        coalesce(nullif(current_setting('app.principal_id', true),
                                        ''), '0')::bigint)
                )
                OR (
                    (coalesce(nullif(current_setting('app.principal_kind', true), ''),
                              'owner')) IN ('member', 'guest')
                    AND owner_id = coalesce(nullif(current_setting('app.house_id', true),
                                                    ''), '0')::bigint
                )
            );
        -- append-only триггер 0001 остаётся единственным способом изменений; INSERT — как
        -- в остальном журнале: household'у своя строка, сервису любая
        CREATE POLICY events_append ON platform.events
            FOR INSERT WITH CHECK (
                (coalesce(nullif(current_setting('app.principal_kind', true), ''),
                          'owner')) = 'service'
                OR owner_id IN (
                    0,
                    coalesce(nullif(current_setting('app.principal_id', true), ''), '0')::bigint,
                    coalesce(nullif(current_setting('app.house_id', true), ''), '0')::bigint
                )
            );
        """
    )
    # «RLS обязано быть» — тот же контур, что в 0005, но про новые таблицы: события получили
    # owner_id, значит получили и политики; «добавили колонку, забыли стену» падает здесь
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_policies
                 WHERE schemaname = 'platform' AND tablename = 'events'
                   AND policyname = 'events_household'
            ) OR NOT (
                SELECT relrowsecurity AND relforcerowsecurity
                  FROM pg_class WHERE oid = 'platform.events'::regclass
            ) THEN
                RAISE EXCEPTION 'platform.events: колонка owner_id есть, а политик нет';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_policies
                 WHERE schemaname = 'platform' AND tablename = 'events'
                   AND policyname = 'events_append'
            ) THEN
                RAISE EXCEPTION 'platform.events: нет политики для INSERT';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS events_append ON platform.events;
        DROP POLICY IF EXISTS events_household ON platform.events;
        ALTER TABLE platform.events DISABLE ROW LEVEL SECURITY;
        DROP TABLE IF EXISTS platform.event_dlq;
        DROP INDEX IF EXISTS platform.outbox_pending_idx;
        DROP INDEX IF EXISTS platform.outbox_event_unique;
        ALTER TABLE platform.outbox
            DROP COLUMN IF EXISTS abandoned_at,
            DROP COLUMN IF EXISTS abandoned_reason;
        ALTER TABLE platform.events
            DROP COLUMN IF EXISTS owner_id,
            DROP COLUMN IF EXISTS schema_version;
        """
    )
