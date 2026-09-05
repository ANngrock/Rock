"""M1 — воспроизводимость: контент-адресованные блобы, цепочка решений, якоря дня.

Что здесь закреплено и почему именно так:

* ``platform.blobs`` — содержимое по его же хэшу: один и тот же промпт/ответ хранится один раз,
  а запись ссылается на 32 байта, а не на «кусочек текста в 8000 символов, надеюсь не обрезалось».
  Обновление запрещено (контент-адрес обязан быть стабильным), удаление оставлено: блоб — не
  журнал, это хранилище, и право на забвение (M6) будет упираться именно в него;
* ``governance.decision_records`` — тот самый журнал решений, append-only и по UPDATE, и по DELETE:
  тихое «INSTEAD NOTHING» из плана превращало бы правку истории в невидимку, а нам нужен шум.
  Разрыв цепочки при этом остаётся различим: удаление записи — это тоже поломка, и она видна по
  пропуску в ``seq``, а не по «всё стало хорошо»;
* ``prev_hash``/``hash`` — цепочка sha256 по всему объёму: доказательство целостности не требует
  доверия к приложению, которое эти строки писало;
* ``governance.anchors`` — мерклово дерево дня. Якорь обновляемый (внешний штамп появляется позже
  самого дерева), поэтому триггера append-only здесь сознательно нет.

Схемы и таблицы живут в ``platform``/``governance``: журнал — инфраструктура доверия, а не память
домена, и доменам писать в него не во что (см. контракты import-linter).
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        -- ---------- функция запрета правок (перезакрепление) ----------

        -- 0001 написала её под platform.events, где есть NEW.id. У platform.blobs идентификатор —
        -- сам sha256, и BEFORE UPDATE падал с «record "new" has no field "id"»: правку это
        -- блокировало, но ошибкой, похожей на поломку схемы. Теперь текст не зависит от того,
        -- есть ли у таблицы колонка id.
        CREATE OR REPLACE FUNCTION platform.forbid_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'таблица %.% append-only (попытка %)',
                TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP
                USING HINT = 'добавь компенсирующее событие вместо правки истории';
        END
        $$;

        -- ---------- хранилище содержимого (контент-адрес) ----------

        CREATE TABLE platform.blobs (
            sha256      bytea       PRIMARY KEY,          -- ровно 32 байта, НЕ hex-строка
            size_bytes  integer     NOT NULL,             -- реальный размер до усечения
            media_type  text        NOT NULL,
            content     bytea       NOT NULL,
            object_key  text,                             -- внешнее хранилище: шаг 11, пока NULL
            created_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT blobs_size_positive CHECK (size_bytes >= 0),
            CONSTRAINT blobs_sha_len CHECK (octet_length(sha256) = 32)
        );
        CREATE INDEX blobs_created_idx ON platform.blobs (created_at);

        CREATE TRIGGER blobs_no_update
            BEFORE UPDATE ON platform.blobs
            FOR EACH ROW EXECUTE FUNCTION platform.forbid_mutation();

        -- ---------- журнал решений: цепочка хэшей ----------

        CREATE TABLE governance.decision_records (
            id               uuid          PRIMARY KEY,
            trace_id         uuid          NOT NULL,
            turn_no          integer       NOT NULL,
            seq              bigint        GENERATED ALWAYS AS IDENTITY,
            kind             text          NOT NULL,
            owner_id         bigint        NOT NULL,
            prompt_ids       jsonb         NOT NULL DEFAULT '[]'::jsonb,
            tools_schema_sha bytea,
            model            text,
            params           jsonb         NOT NULL DEFAULT '{}'::jsonb,
            input_sha        bytea,
            output_sha       bytea,
            policy           jsonb,
            cost_usd         numeric(12,6) NOT NULL DEFAULT 0,
            latency_ms       integer       NOT NULL DEFAULT 0,
            truncated        boolean       NOT NULL DEFAULT false,
            note             text,
            prev_hash        bytea         NOT NULL,
            hash             bytea         NOT NULL,
            created_at       timestamptz   NOT NULL DEFAULT now(),
            CONSTRAINT decision_records_kind CHECK
                (kind IN ('llm_call', 'tool_run', 'policy', 'turn_summary')),
            CONSTRAINT decision_records_hash_len CHECK
                (octet_length(hash) = 32 AND octet_length(prev_hash) = 32),
            CONSTRAINT decision_records_hash_not_self CHECK (hash <> prev_hash),
            CONSTRAINT decision_records_turn CHECK (turn_no >= 0)
        );
        CREATE INDEX decision_records_trace_idx ON governance.decision_records (trace_id, turn_no);
        CREATE INDEX decision_records_seq_idx ON governance.decision_records (seq);
        CREATE INDEX decision_records_day_idx ON governance.decision_records (created_at);
        -- «чем ответил и сколько это стоило» выбирают по ходам, а не сканом по всем видам записей
        CREATE INDEX decision_records_turns_idx ON governance.decision_records (created_at)
            WHERE kind = 'turn_summary';

        CREATE TRIGGER decision_records_append_only
            BEFORE UPDATE OR DELETE ON governance.decision_records
            FOR EACH ROW EXECUTE FUNCTION platform.forbid_mutation();

        -- ---------- якорь дня: мерклово дерево над цепочкой ----------

        CREATE TABLE governance.anchors (
            day         date        PRIMARY KEY,
            first_seq   bigint      NOT NULL,
            last_seq    bigint      NOT NULL,
            records     integer     NOT NULL,
            merkle_root bytea       NOT NULL,
            method      text        NOT NULL DEFAULT 'sha256-pairwise',
            ots_proof   bytea,
            anchored_at timestamptz NOT NULL DEFAULT now(),
            external_at timestamptz,
            verified_at timestamptz,
            note        text,
            CONSTRAINT anchors_root_len CHECK (octet_length(merkle_root) = 32),
            CONSTRAINT anchors_range CHECK (last_seq >= first_seq AND records >= 0)
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS governance.anchors CASCADE;
        DROP TABLE IF EXISTS governance.decision_records CASCADE;
        DROP TABLE IF EXISTS platform.blobs CASCADE;

        -- определение 0001 обратно: blobs больше нет, и ссылка на NEW.id снова безопасна
        CREATE OR REPLACE FUNCTION platform.forbid_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'таблица %.% append-only (попытка %: %)',
                TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP, COALESCE(NEW.id::text, OLD.id::text)
                USING HINT = 'добавь компенсирующее событие вместо правки истории';
        END
        $$;
        """
    )
