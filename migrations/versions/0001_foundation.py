"""Шаг 1 — фундамент: схемы platform / governance / memory / knowledge.

Что здесь закреплено и почему именно так:

* схемы по доменам в одной БД (ADR-003): граница видна в SQL, а выделение сервисов — отдельное
  решение, а не дефолт;
* ``platform.events`` — append-only на уровне триггера (не «правило, которое молча глотает
  UPDATE»): изменение прошлого события должно быть ошибкой, иначе event sourcing — фикция;
* outbox в той же БД и той же транзакции (ADR-004) — событие не может «потеряться» между
  записью состояния и публикацией;
* ``governance.tool_runs`` и ``platform.llm_calls`` — трасса каждого решения и каждого вызова
  модели: воспроизводимость (принцип 4) и разбивка бюджета по дням через вьюху;
* вектора — pgvector + GIN-trigram для текстового фолбэка: поиск обязан работать и без эмбеддингов.

"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        CREATE EXTENSION IF NOT EXISTS pgcrypto;

        CREATE SCHEMA IF NOT EXISTS platform;
        CREATE SCHEMA IF NOT EXISTS governance;
        CREATE SCHEMA IF NOT EXISTS memory;
        CREATE SCHEMA IF NOT EXISTS knowledge;

        -- ---------- event store ----------

        CREATE TABLE platform.events (
            id          bigserial    PRIMARY KEY,
            stream_type text         NOT NULL,
            stream_id   text         NOT NULL,
            version     integer      NOT NULL CHECK (version > 0),
            event_type  text         NOT NULL,
            payload     jsonb        NOT NULL DEFAULT '{}'::jsonb,
            metadata    jsonb        NOT NULL DEFAULT '{}'::jsonb,
            occurred_at timestamptz  NOT NULL DEFAULT now(),
            CONSTRAINT events_stream_version UNIQUE (stream_id, version)
        );
        CREATE INDEX events_stream_time_idx ON platform.events (stream_type, occurred_at);
        CREATE INDEX events_type_idx ON platform.events (event_type);

        -- Append-only: UPDATE/DELETE — ошибка, а не тихое ничего.
        CREATE FUNCTION platform.forbid_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'таблица %.% append-only (попытка %: %)',
                TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP, COALESCE(NEW.id::text, OLD.id::text)
                USING HINT = 'добавь компенсирующее событие вместо правки истории';
        END
        $$;
        CREATE TRIGGER events_append_only
            BEFORE UPDATE OR DELETE ON platform.events
            FOR EACH ROW EXECUTE FUNCTION platform.forbid_mutation();

        -- ---------- transactional outbox ----------

        CREATE TABLE platform.outbox (
            id           bigserial   PRIMARY KEY,
            event_id     bigint      NOT NULL REFERENCES platform.events (id) ON DELETE CASCADE,
            published_at timestamptz,
            attempts     integer     NOT NULL DEFAULT 0,
            last_error   text
        );
        -- частичный индекс: публикуем только непубликованное, и не сканируем историю
        CREATE INDEX outbox_unpublished_idx ON platform.outbox (id) WHERE published_at IS NULL;

        -- ---------- трассировка моделей и инструментов ----------

        CREATE TABLE platform.llm_calls (
            id                 bigserial    PRIMARY KEY,
            call_id            uuid         NOT NULL,
            trace_id           uuid         NOT NULL,
            role               text         NOT NULL,
            model              text         NOT NULL,
            provider           text         NOT NULL DEFAULT 'primary',
            attempt            integer      NOT NULL DEFAULT 0,
            prompt_tokens      integer      NOT NULL DEFAULT 0,
            completion_tokens  integer      NOT NULL DEFAULT 0,
            cost_usd           numeric(12,6) NOT NULL DEFAULT 0,
            latency_ms         integer      NOT NULL DEFAULT 0,
            ok                 boolean      NOT NULL,
            error              text,
            created_at         timestamptz  NOT NULL DEFAULT now()
        );
        CREATE INDEX llm_calls_created_idx ON platform.llm_calls (created_at);
        CREATE INDEX llm_calls_trace_idx ON platform.llm_calls (trace_id);

        CREATE TABLE governance.tool_runs (
            id         bigserial   PRIMARY KEY,
            trace_id   uuid        NOT NULL,
            owner_id   bigint      NOT NULL,
            tool       text        NOT NULL,
            args       jsonb       NOT NULL DEFAULT '{}'::jsonb,
            decision   text        NOT NULL,
            result     text,
            ok         boolean     NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX tool_runs_trace_idx ON governance.tool_runs (trace_id);
        CREATE INDEX tool_runs_tool_time_idx ON governance.tool_runs (tool, created_at);

        CREATE VIEW platform.v_cost_daily AS
        SELECT date_trunc('day', created_at)::date          AS day,
               role,
               model,
               count(*)                                      AS calls,
               sum(prompt_tokens)                            AS prompt_tokens,
               sum(completion_tokens)                        AS completion_tokens,
               sum(cost_usd)                                 AS cost_usd,
               (avg(latency_ms) / 1000.0)::numeric(10, 3) AS avg_latency_s,
               count(*) FILTER (WHERE NOT ok)                AS failures
        FROM platform.llm_calls
        GROUP BY 1, 2, 3;

        -- ---------- память: факты о владельце ----------

        CREATE TABLE memory.facts (
            id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            fact       text        NOT NULL,
            category   text        NOT NULL DEFAULT 'general',
            importance real        NOT NULL DEFAULT 0.5 CHECK (importance >= 0 AND importance <= 1),
            source     text        NOT NULL,
            embedding  vector(2048),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            valid_to   timestamptz,
            CONSTRAINT facts_fact_unique UNIQUE (fact)
        );
        -- актуальные (valid_to IS NULL) — то, что собирается в системный промпт
        CREATE INDEX facts_active_idx ON memory.facts (importance DESC, created_at DESC)
            WHERE valid_to IS NULL;

        -- ---------- знания: заметки и сохранённые страницы ----------

        CREATE TABLE knowledge.notes (
            id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            title      text        NOT NULL,
            body       text        NOT NULL DEFAULT '',
            tags       text[]      NOT NULL DEFAULT '{}',
            source     text        NOT NULL,
            raw_input  text,
            embedding  vector(2048),
            indexed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX notes_tags_idx ON knowledge.notes USING gin (tags);
        CREATE INDEX notes_title_trgm_idx ON knowledge.notes USING gin (title gin_trgm_ops);
        CREATE INDEX notes_body_trgm_idx ON knowledge.notes USING gin (body gin_trgm_ops);
        -- ВАЖНО: ANN-индексы pgvector (hnsw/ivfflat) работают только для vector(<= 2000)
        -- измерений, а embedding-3 отдаёт 2048 — индекс на колонке embedding создать нельзя
        -- (Postgres отвечает «column cannot have more than 2000 dimensions for hnsw index»).
        -- На личном объёме (тысячи заметок) точный косинус-скан стоит единицы миллисекунд,
        -- поэтому индекс здесь отсутствует осознанно. Когда база вырастет, вариантов два:
        -- попросить у embedding-3 размерность 1024 (dimensions=..., Matryoshka) и вернуть
        -- hnsw, либо перейти на halfvec + hnsw (лимит 4000).
        -- частичный индекс нужен индексатору: «найти незаиндексированные» без полного скана
        CREATE INDEX notes_unindexed_idx ON knowledge.notes (created_at) WHERE embedding IS NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP VIEW IF EXISTS platform.v_cost_daily;
        DROP TABLE IF EXISTS knowledge.notes CASCADE;
        DROP TABLE IF EXISTS memory.facts CASCADE;
        DROP TABLE IF EXISTS governance.tool_runs CASCADE;
        DROP TABLE IF EXISTS platform.llm_calls CASCADE;
        DROP TABLE IF EXISTS platform.outbox CASCADE;
        DROP TABLE IF EXISTS platform.events CASCADE;
        DROP FUNCTION IF EXISTS platform.forbid_mutation();
        DROP SCHEMA IF EXISTS knowledge;
        DROP SCHEMA IF EXISTS memory;
        DROP SCHEMA IF EXISTS governance;
        DROP SCHEMA IF EXISTS platform;
        """
    )
