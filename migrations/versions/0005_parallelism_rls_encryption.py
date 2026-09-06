"""0005: параллельность (F1), принципы/RLS (F2), конвертовое шифрование (F3), флаги/SLO/метрики
(F6/F7), activity-реестр (F8) — и «RLS как свойство схемы», а не как договорённость.

Почему одна миграция: это один разрезающий слой «данные знают, КТО они смотрит». Половинки
(RLS без GUC-хука, таблицы без политик) оставляют базу в состоянии «проверка есть, но ловит
не всё», а состояние «RLS включён наполовину» — хуже, чем выключенный: он усыпляет бдительность.

Порядок блоков: сначала новые таблицы (их ничто не трогает), затем ALTER существующих
(additive-колонки, новые constraints), затем триггер блобов (заменяется ПОЛНОСТЬЮ — «можно
менять только ключевые колонки»), в конце — политики + финальный DO-assert, что ни одна таблица
с owner_id не осталась без политик. Assert в конце — это и есть CI-механизм «миграция обязана
упасть, если RLS забыт»: не линтер снаружи, а свойство самой миграции.

Все изменения — expand-фазы (F10): ни одного rename/drop колонки, которую читает предыдущая
ревизия. Триггер блобов — единственный «заменяемый» объект, и его новая версия строже-плюс
(запрещает всё, кроме колонки ключа), а не «просто снят».
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Preamble (F10): DDL ждёт lock'ы секунд пять и уходит с внятной ошибкой, а не вешает
    # трафик; в alembic-транзакции это SET LOCAL — переживает только эту миграцию.
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        """
    )

    # ============ F1: заявки хода, очередь «один на владельца», дедуп update_id ============

    op.execute(
        """
        -- monotonный счётчик токенов: «чей ход живой» решается не временем, а порядком.
        CREATE SEQUENCE IF NOT EXISTS governance.fencing_seq;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS governance.turn_claims (
            trace_id         uuid        PRIMARY KEY,
            owner_id         bigint      NOT NULL,
            status           text        NOT NULL DEFAULT 'active'
                             CHECK (status IN ('active', 'finished', 'dropped')),
            step             integer     NOT NULL DEFAULT 0 CHECK (step >= 0),
            fencing_token    bigint      NOT NULL,
            prompt_ids       jsonb       NOT NULL DEFAULT '[]'::jsonb,
            tools_schema_sha bytea,
            lease_until      timestamptz NOT NULL,
            finished_at      timestamptz,      -- «когда ход закрылся»: re-open сверяется с этим
            created_at       timestamptz NOT NULL DEFAULT now(),
            updated_at       timestamptz NOT NULL DEFAULT now()
        );
        -- «один активный ход на владельца» — частью УНИКАЛЬНОГО индекса, а не кодом:
        -- два процесса, вставляющие одновременно, разминутся на конфликте, а не на гонке
        CREATE UNIQUE INDEX IF NOT EXISTS turn_claims_one_active
            ON governance.turn_claims (owner_id) WHERE status = 'active';
        CREATE INDEX IF NOT EXISTS turn_claims_lease_idx
            ON governance.turn_claims (lease_until) WHERE status = 'active';
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS governance.turn_queue (
            id         bigserial   PRIMARY KEY,
            owner_id   bigint      NOT NULL,
            trace_id   uuid        NOT NULL,
            kind       text        NOT NULL,
            payload    jsonb       NOT NULL DEFAULT '{}'::jsonb,
            status     text        NOT NULL DEFAULT 'queued'
                       CHECK (status IN ('queued', 'done', 'failed')),
            attempts   integer     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            created_at timestamptz NOT NULL DEFAULT now()
        );
        -- drain берёт строго по порядку и только «queued»: частичный индекс — это и очередь,
        -- и гарантия «старое сообщение не обгонит новое»
        CREATE INDEX IF NOT EXISTS turn_queue_pending_idx
            ON governance.turn_queue (owner_id, id) WHERE status = 'queued';
        """
    )
    op.execute(
        """
        -- дедуп Telegram-апдейтов (F1): «бот получил апдейт дважды» перестаёт быть вопросом
        -- памяти процесса. seen_at без TTL-политики: таблица маленькая, а «кто когда видел» —
        -- диагностика инцидента «почему не ответили»
        CREATE TABLE IF NOT EXISTS platform.telegram_updates (
            update_id bigint      NOT NULL PRIMARY KEY,
            chat_id   bigint      NOT NULL,
            kind      text        NOT NULL DEFAULT 'message',
            seen_at   timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS telegram_updates_seen_idx
            ON platform.telegram_updates (seen_at);
        """
    )

    # ============ F2: actor в журнале и в аудите ============

    op.execute(
        """
        ALTER TABLE governance.decision_records
            ADD COLUMN IF NOT EXISTS actor_id bigint NOT NULL DEFAULT 0;
        ALTER TABLE governance.tool_runs
            ADD COLUMN IF NOT EXISTS actor_id bigint NOT NULL DEFAULT 0;
        -- «сколько потратил принципал сегодня» — точечный запрос на каждом ходе:
        CREATE INDEX IF NOT EXISTS decision_records_actor_day_idx
            ON governance.decision_records (actor_id, created_at);
        CREATE INDEX IF NOT EXISTS decision_records_owner_day_idx
            ON governance.decision_records (owner_id, created_at);
        -- kinds расширяются (verdict/system/tombstone — F3 пишет tombstone именно сюда);
        -- константа перечисляет ВСЕ, включая прежние: старые строки остаются валидными
        ALTER TABLE governance.decision_records
            DROP CONSTRAINT IF EXISTS decision_records_kind;
        ALTER TABLE governance.decision_records
            ADD CONSTRAINT decision_records_kind CHECK
                (kind IN ('llm_call', 'tool_run', 'policy', 'turn_summary',
                          'verdict', 'system', 'tombstone'));
        """
    )

    # ============ F3: конвертовое шифрование блобов + хранилище ключей-обёрток ============

    op.execute(
        """
        -- wrapped_dek/ключ живут ОТДЕЛЬНО от содержимого: крипто-стирание = удалить DEK,
        -- и «content_cipher='none'» — легитимное состояние dev, а не дыра.
        ALTER TABLE platform.blobs
            ADD COLUMN IF NOT EXISTS wrapped_dek bytea,
            ADD COLUMN IF NOT EXISTS key_version integer NOT NULL DEFAULT 1,
            ADD COLUMN IF NOT EXISTS content_cipher text NOT NULL DEFAULT 'none';
        CREATE INDEX IF NOT EXISTS blobs_key_version_idx
            ON platform.blobs (key_version) WHERE content_cipher <> 'none';
        """
    )
    op.execute(
        """
        -- Триггер 0002 запрещал ЛЮБОЙ update блоба. Ротация ключа (F3) обязана менять
        -- wrapped_dek/key_version, и «запрещено всё» пришлось бы снимать целиком — а это
        -- открыло бы переписывание содержимого. Новая функция точечная: содержимое по-прежнему
        -- неизменяемо, ротация — единственная разрешённая правка.
        CREATE OR REPLACE FUNCTION platform.blobs_key_rotation_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.sha256 IS DISTINCT FROM OLD.sha256
               OR NEW.content IS DISTINCT FROM OLD.content
               OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
               OR NEW.media_type IS DISTINCT FROM OLD.media_type
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.object_key IS DISTINCT FROM OLD.object_key
               OR NEW.content_cipher IS DISTINCT FROM OLD.content_cipher
            THEN
                RAISE EXCEPTION 'таблица platform.blobs append-only (попытка %)', TG_OP
                    USING HINT = 'разрешены только wrapped_dek/key_version (ротация ключа)';
            END IF;
            RETURN NEW;
        END
        $$;
        DROP TRIGGER IF EXISTS blobs_no_update ON platform.blobs;
        CREATE TRIGGER blobs_key_rotation
            BEFORE UPDATE ON platform.blobs
            FOR EACH ROW EXECUTE FUNCTION platform.blobs_key_rotation_only();
        """
    )
    op.execute(
        """
        -- журнал стёртых ключей: строка остаётся ПОСЛЕ удаления блоба — «что уничтожено и по
        -- какому манифесту» переживает сами данные (доказательство факта уничтожения)
        CREATE TABLE IF NOT EXISTS platform.key_shreds (
            id           bigserial   PRIMARY KEY,
            blob_sha     bytea       NOT NULL UNIQUE CHECK (octet_length(blob_sha) = 32),
            key_version  integer     NOT NULL,
            reason       text        NOT NULL DEFAULT '',
            actor_id     bigint      NOT NULL DEFAULT 0,
            manifest     jsonb       NOT NULL DEFAULT '{}'::jsonb,
            manifest_sha bytea       CHECK
                           (manifest_sha IS NULL OR octet_length(manifest_sha) = 32),
            shredded_at  timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS platform.legal_holds (
            id           bigserial   PRIMARY KEY,
            scope        text        NOT NULL CHECK (scope IN ('class', 'principal')),
            value        text        NOT NULL,
            reason       text        NOT NULL DEFAULT '',
            placed_by    bigint      NOT NULL DEFAULT 0,
            placed_at    timestamptz NOT NULL DEFAULT now(),
            released_by  bigint,
            released_at  timestamptz
        );
        -- «hold активен» — частично-уникально: повторное размещение идемпотентно (ON CONFLICT
        -- DO NOTHING), история снятых хоулдов остаётся
        CREATE UNIQUE INDEX IF NOT EXISTS legal_holds_active_idx
            ON platform.legal_holds (scope, value) WHERE released_at IS NULL;
        """
    )

    # ============ F8: реестр активностей (идемпотентность сайд-эффектов) ============

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.activity_ledger (
            activity_id   text        PRIMARY KEY,
            trace_id      uuid,
            state         text        NOT NULL
                          CHECK (state IN ('running', 'done', 'failed', 'compensated')),
            fencing_token bigint      NOT NULL DEFAULT 0,
            owner         text        NOT NULL DEFAULT '',
            attempts      integer     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            result        jsonb,
            last_error    text,
            started_at    timestamptz NOT NULL DEFAULT now(),
            completed_at  timestamptz
        );
        -- reap подметает чужие висяки; частичный индекс — чтобы «обычный» путь его не трогал
        CREATE INDEX IF NOT EXISTS activity_ledger_running_idx
            ON platform.activity_ledger (started_at) WHERE state = 'running';
        """
    )

    # ============ F2: принципы, гранты, состояние ============

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.principals (
            principal_id bigint      NOT NULL PRIMARY KEY,
            subject      text        NOT NULL DEFAULT '',
            kind         text        NOT NULL DEFAULT 'guest'
                         CHECK (kind IN ('owner', 'member', 'guest', 'service')),
            display_name text        NOT NULL DEFAULT '',
            status       text        NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'killed', 'disabled')),
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS platform.principal_grants (
            principal_id bigint      NOT NULL
                         REFERENCES platform.principals (principal_id) ON DELETE CASCADE,
            action       text        NOT NULL,
            granted_by   bigint      NOT NULL DEFAULT 0,
            granted_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (principal_id, action)
        );
        CREATE TABLE IF NOT EXISTS platform.principal_state (
            principal_id      bigint        NOT NULL PRIMARY KEY
                              REFERENCES platform.principals (principal_id) ON DELETE CASCADE,
            kill_switch       jsonb         NOT NULL DEFAULT '{"active": false}'::jsonb,
            daily_budget_usd  numeric(12,4),
            updated_at        timestamptz   NOT NULL DEFAULT now()
        );
        """
    )

    # ============ F6: флаги; F7: метрики и runtime-overrides; F10: backfills ============

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS platform.feature_flags (
            key              text        NOT NULL PRIMARY KEY,
            percent          integer     NOT NULL DEFAULT 0 CHECK (percent BETWEEN 0 AND 100),
            allow_principals bigint[]    NOT NULL DEFAULT '{}',
            deny_principals  bigint[]    NOT NULL DEFAULT '{}',
            stage            text        NOT NULL DEFAULT 'shadow'
                             CHECK (stage IN ('shadow', 'canary', 'full', 'retired')),
            description      text        NOT NULL DEFAULT '',
            created_at       timestamptz NOT NULL DEFAULT now(),
            updated_at       timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS platform.runtime_overrides (
            key          text        NOT NULL PRIMARY KEY,
            value        jsonb       NOT NULL,
            reason       text        NOT NULL DEFAULT '',
            principal_id bigint,
            set_at       timestamptz NOT NULL DEFAULT now(),
            expires_at   timestamptz
        );
        CREATE TABLE IF NOT EXISTS platform.metric_samples (
            id           bigserial   PRIMARY KEY,
            name         text        NOT NULL,
            kind         text        NOT NULL,
            labels       jsonb       NOT NULL DEFAULT '{}'::jsonb,
            count        double precision NOT NULL DEFAULT 0,
            sum_value    double precision,
            buckets      jsonb,
            counts       jsonb,
            period_start timestamptz NOT NULL,
            period_end   timestamptz NOT NULL,
            source       text        NOT NULL DEFAULT 'aegis',
            created_at   timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS metric_samples_name_period_idx
            ON platform.metric_samples (name, period_end DESC);
        """
    )
    op.execute(
        """
        -- фоновые доналивки (F10): курсор в jsonb («форма» зависит от задания), аренда — та же
        -- схема, что у reminder'ов: два процесса не потянут один батч
        CREATE TABLE IF NOT EXISTS platform.backfills (
            name        text        NOT NULL PRIMARY KEY,
            target      text        NOT NULL,
            batch_size  integer     NOT NULL DEFAULT 500 CHECK (batch_size > 0),
            cursor      jsonb       NOT NULL DEFAULT '{}'::jsonb,
            status      text        NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'running', 'paused', 'done', 'failed')),
            rows_done   bigint      NOT NULL DEFAULT 0,
            rounds      integer     NOT NULL DEFAULT 0,
            last_error  text,
            lease_owner text,
            lease_until timestamptz,
            updated_at  timestamptz NOT NULL DEFAULT now()
        );
        -- регистрация заданий идемпотентна:应用 каталога spec'ов при каждом старте
        INSERT INTO platform.backfills (name, target, batch_size) VALUES
            ('events_owner', 'platform.events.owner_id из stream_id', 1000),
            ('journal_actor', 'governance.decision_records.actor_id = owner_id', 1000)
        ON CONFLICT (name) DO NOTHING;
        """
    )

    # ============ F2: RLS ============

    # facts/notes писались «под одного владельца» и колонки owner_id не имели; без неё RLS
    # не выразить вообще. 0 = «наследие общего домохозяйства»: видно владельцу (и member для
    # фактов — они и были общими), не видно гостю. Заполнение по-настоящему — job, а не
    # миграция (F10: backfill на аренде; здесь только DEFAULT)
    op.execute(
        """
        ALTER TABLE memory.facts
            ADD COLUMN IF NOT EXISTS owner_id bigint NOT NULL DEFAULT 0;
        """
    )

    op.execute(
        """
        -- Политики читают три GUC (app.principal_id / app.principal_kind / app.house_id),
        -- которые ставит хук начала транзакции. «unset» трактуется как owner@0: пусто и тихо,
        -- а не «видно всё» — невидимый промах безопаснее видимого leak.
        ALTER TABLE governance.decision_records ENABLE ROW LEVEL SECURITY;
        ALTER TABLE governance.decision_records FORCE ROW LEVEL SECURITY;
        ALTER TABLE governance.tool_runs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE governance.tool_runs FORCE ROW LEVEL SECURITY;
        ALTER TABLE planning.reminders ENABLE ROW LEVEL SECURITY;
        ALTER TABLE planning.reminders FORCE ROW LEVEL SECURITY;
        """
    )
    op.execute(
        """
        CREATE POLICY decision_records_household ON governance.decision_records
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
                              'owner')) = 'member'
                    AND (owner_id = coalesce(nullif(current_setting('app.house_id', true),
                                                     ''), '0')::bigint
                         OR actor_id = coalesce(nullif(current_setting('app.principal_id', true),
                                                        ''), '0')::bigint)
                )
                OR (
                    (coalesce(nullif(current_setting('app.principal_kind', true), ''),
                              'owner')) = 'guest'
                    AND actor_id = coalesce(nullif(current_setting('app.principal_id', true),
                                                   ''), '0')::bigint
                )
            );
        """
    )
    op.execute(
        """
        -- запись журнала доступна любому связанному принципалу household'а: гость обязан
        -- иметь возможность СОЗДАТЬ строку (её увидят владелец и он сам по actor_id), но не
        -- чинить/удалять чужие строки
        CREATE POLICY decision_records_append ON governance.decision_records
            FOR INSERT WITH CHECK (
                (coalesce(nullif(current_setting('app.principal_kind', true), ''),
                          'owner')) = 'service'
                OR owner_id IN (
                    0,
                    coalesce(nullif(current_setting('app.principal_id', true), ''), '0')::bigint,
                    coalesce(nullif(current_setting('app.house_id', true), ''), '0')::bigint
                )
            );
        CREATE POLICY decision_records_repair ON governance.decision_records
            FOR UPDATE USING (
                (coalesce(nullif(current_setting('app.principal_kind', true), ''), 'owner'))
                = 'service'
            )
            WITH CHECK (
                (coalesce(nullif(current_setting('app.principal_kind', true), ''), 'owner'))
                = 'service'
            );
        """
    )
    op.execute(
        """
        CREATE POLICY tool_runs_household ON governance.tool_runs
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
                    AND (owner_id = coalesce(nullif(current_setting('app.house_id', true),
                                                     ''), '0')::bigint
                         OR actor_id = coalesce(nullif(current_setting('app.principal_id', true),
                                                        ''), '0')::bigint)
                )
            );
        CREATE POLICY tool_runs_append ON governance.tool_runs
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
    op.execute(
        """
        -- напоминания: гость не видит чужие, member — только household-свои; тик демона
        -- работает под service'ом
        CREATE POLICY reminders_household ON planning.reminders
            FOR SELECT USING (
                (coalesce(nullif(current_setting('app.principal_kind', true), ''), 'owner'))
                    = 'service'
                OR ((coalesce(nullif(current_setting('app.principal_kind', true), ''), 'owner'))
                        = 'owner'
                    AND owner_id IN (0, coalesce(nullif(current_setting('app.principal_id',
                                                                         true), ''), '0')::bigint))
                OR ((coalesce(nullif(current_setting('app.principal_kind', true), ''), 'owner'))
                        = 'member'
                    AND owner_id IN (
                        0,
                        coalesce(nullif(current_setting('app.house_id', true),
                                        ''), '0')::bigint,
                        coalesce(nullif(current_setting('app.principal_id', true),
                                        ''), '0')::bigint
                    ))
                OR ((coalesce(nullif(current_setting('app.principal_kind', true), ''), 'owner'))
                        = 'guest'
                    AND owner_id = coalesce(nullif(current_setting('app.principal_id', true),
                                                    ''), '0')::bigint)
            );
        -- постановка напоминания — не привилегия «только владельца»: ход гостя вправе
        -- поставить household-напоминание, если policy-движок разрешил инструмент
        CREATE POLICY reminders_append ON planning.reminders
            FOR INSERT WITH CHECK (
                owner_id IN (
                    0,
                    coalesce(nullif(current_setting('app.principal_id', true), ''), '0')::bigint,
                    coalesce(nullif(current_setting('app.house_id', true), ''), '0')::bigint
                )
            );
        CREATE POLICY reminders_write ON planning.reminders
            FOR UPDATE USING (
                (coalesce(nullif(current_setting('app.principal_kind', true), ''), 'owner'))
                    IN ('service', 'owner')
                OR owner_id = coalesce(nullif(current_setting('app.principal_id', true),
                                              ''), '0')::bigint
            )
            WITH CHECK (
                (coalesce(nullif(current_setting('app.principal_kind', true), ''), 'owner'))
                    IN ('service', 'owner')
                OR owner_id = coalesce(nullif(current_setting('app.principal_id', true),
                                              ''), '0')::bigint
            );
        """
    )
    op.execute(
        """
        CREATE POLICY facts_household ON memory.facts
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
                              'owner')) = 'member'
                    AND owner_id IN (0,
                        coalesce(nullif(current_setting('app.house_id', true),
                                        ''), '0')::bigint)
                )
            );
        -- гость — только чтение household-фактов; правки памяти через memory:write грант
        -- недоступны гостям и RLS это дублирует: две независимые стены
        CREATE POLICY facts_append ON memory.facts
            FOR INSERT WITH CHECK (
                (coalesce(nullif(current_setting('app.principal_kind', true), ''), 'owner'))
                    = 'service'
                OR (
                    (coalesce(nullif(current_setting('app.principal_kind', true), ''), 'owner'))
                    <> 'guest'
                    AND owner_id IN (
                        0,
                        coalesce(nullif(current_setting('app.principal_id', true),
                                        ''), '0')::bigint,
                        coalesce(nullif(current_setting('app.house_id', true),
                                        ''), '0')::bigint
                    )
                )
            );
        CREATE POLICY facts_repair ON memory.facts
            FOR UPDATE USING (
                (coalesce(nullif(current_setting('app.principal_kind', true), ''), 'owner'))
                    IN ('service', 'owner')
            )
            WITH CHECK (
                (coalesce(nullif(current_setting('app.principal_kind', true), ''), 'owner'))
                    IN ('service', 'owner')
            );
        ALTER TABLE memory.facts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE memory.facts FORCE ROW LEVEL SECURITY;
        """
    )

    # роль-«глаза гостя» для тестов: superuser RLS обходит, поэтому свойство «гость не
    # видит чужое» проверяется ТОЛЬКО под непривилегированной ролью. NOINHERIT — свои гранты
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aegis_rls_ro') THEN
                CREATE ROLE aegis_rls_ro NOLOGIN NOINHERIT;
            END IF;
        END
        $$;
        GRANT USAGE ON SCHEMA governance, platform, memory, planning TO aegis_rls_ro;
        GRANT SELECT ON governance.decision_records, governance.tool_runs,
                        planning.reminders, memory.facts, governance.turn_claims
                        TO aegis_rls_ro;
        """
    )

    # ============ Финальный контур: «таблица с owner_id без политики» = падение миграции ====

    op.execute(
        """
        DO $$
        DECLARE
            rec record;
        BEGIN
            FOR rec IN
                SELECT c.oid AS oid, c.oid::regclass AS tbl,
                       n.nspname AS schema, c.relname AS name
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname IN ('governance', 'platform', 'memory', 'knowledge')
                   AND c.relkind = 'r'
                   AND EXISTS (
                       SELECT 1 FROM pg_attribute a
                        WHERE a.attrelid = c.oid
                          AND a.attname = 'owner_id' AND a.attnum > 0 AND NOT a.attisdropped
                   )
                   -- координационные таблицы (заявки/очередь хода) вне RLS по design: они
                   -- про «кто исполняет», а не про «чья контентная строка»; события/outbox
                   -- получат owner_id+политики в 0006 (там же, где сами колонки)
                   AND c.relname NOT IN ('turn_claims', 'turn_queue')
            LOOP
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies p
                     WHERE p.schemaname = rec.schema AND p.tablename = rec.name
                ) THEN
                    RAISE EXCEPTION
                        'RLS забыт для %: таблица с owner_id обязана иметь политики', rec.tbl;
                END IF;
                IF NOT (SELECT relrowsecurity FROM pg_class WHERE oid = rec.oid)
                   OR NOT (SELECT relforcerowsecurity FROM pg_class WHERE oid = rec.oid)
                THEN  -- ENABLE/FORCE缺一不可: без FORCE политики обходятся владельцем таблицы
                    RAISE EXCEPTION 'RLS для % включён неполно (нужны ENABLE+FORCE)', rec.tbl;
                END IF;
            END LOOP;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS facts_append ON memory.facts;
        DROP POLICY IF EXISTS facts_household ON memory.facts;
        ALTER TABLE memory.facts DISABLE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS reminders_write ON planning.reminders;
        DROP POLICY IF EXISTS reminders_household ON planning.reminders;
        ALTER TABLE planning.reminders DISABLE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS tool_runs_append ON governance.tool_runs;
        DROP POLICY IF EXISTS tool_runs_household ON governance.tool_runs;
        ALTER TABLE governance.tool_runs DISABLE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS decision_records_repair ON governance.decision_records;
        DROP POLICY IF EXISTS decision_records_append ON governance.decision_records;
        DROP POLICY IF EXISTS decision_records_household ON governance.decision_records;
        ALTER TABLE governance.decision_records DISABLE ROW LEVEL SECURITY;
        """
    )
    op.execute(
        """
        DROP TABLE IF EXISTS platform.backfills;
        DROP TABLE IF EXISTS platform.metric_samples;
        DROP TABLE IF EXISTS platform.runtime_overrides;
        DROP TABLE IF EXISTS platform.feature_flags;
        DROP TABLE IF EXISTS platform.principal_state;
        DROP TABLE IF EXISTS platform.principal_grants;
        DROP TABLE IF EXISTS platform.principals;
        DROP TABLE IF EXISTS platform.activity_ledger;
        DROP TABLE IF EXISTS platform.legal_holds;
        DROP TABLE IF EXISTS platform.key_shreds;
        DROP TRIGGER IF EXISTS blobs_key_rotation ON platform.blobs;
        DROP FUNCTION IF EXISTS platform.blobs_key_rotation_only();
        CREATE TRIGGER blobs_no_update
            BEFORE UPDATE ON platform.blobs
            FOR EACH ROW EXECUTE FUNCTION platform.forbid_mutation();
        ALTER TABLE platform.blobs
            DROP COLUMN IF EXISTS wrapped_dek,
            DROP COLUMN IF EXISTS key_version,
            DROP COLUMN IF EXISTS content_cipher;
        ALTER TABLE governance.tool_runs DROP COLUMN IF EXISTS actor_id;
        -- append-only триггер мешает вычистить новые kind'и — снимается на время отката
        DROP TRIGGER IF EXISTS decision_records_append_only ON governance.decision_records;
        DELETE FROM governance.decision_records
            WHERE kind IN ('verdict', 'system', 'tombstone');
        ALTER TABLE governance.decision_records
            DROP CONSTRAINT IF EXISTS decision_records_kind;
        ALTER TABLE governance.decision_records
            ADD CONSTRAINT decision_records_kind CHECK
                (kind IN ('llm_call', 'tool_run', 'policy', 'turn_summary'));
        ALTER TABLE governance.decision_records DROP COLUMN IF EXISTS actor_id;
        CREATE TRIGGER decision_records_append_only
            BEFORE UPDATE OR DELETE ON governance.decision_records
            FOR EACH ROW EXECUTE FUNCTION platform.forbid_mutation();
        DROP TABLE IF EXISTS platform.telegram_updates;
        DROP TABLE IF EXISTS governance.turn_queue;
        DROP TABLE IF EXISTS governance.turn_claims;
        DROP SEQUENCE IF EXISTS governance.fencing_seq;
        """
    )
