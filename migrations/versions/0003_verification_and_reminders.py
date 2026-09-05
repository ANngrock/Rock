"""Шаг 2 — верификация ответов и напоминания: новый вид журнала + рабочее расписание.

Почему это отдельная миграция, а не правка 0002: та уже накатана на базу владельца, и изменённый
прошлый шаг оставил бы схему и код вразнобой (проверить нечем — база-то старая).

* ``decision_records_kind`` расширяется значением ``verdict``: вердикт верификатора — такое же
  событие хода, как решение политики. Кидать его в ``note`` соседней записи означало бы потерять
  главное: «проверено и сошлось» и «не проверялось вовсе» обязаны различаться запросом, иначе
  «почему ты так ответил» не сможет сказать, проверялся ли ответ вообще.
* ``governance.reminders`` — НЕ append-only, и это сознательно: статус меняется (``scheduled →
  sent``), а append-only здесь означал бы, что отменить напоминание невозможно. История хода при
  этом не страдает: факт постановки и отмены живёт в журнале и event store, а эта таблица — рабочее
  расписание, которое исполняет tick.
* ``attempts``/``last_error``: доставка в Telegram может не удаться (сеть, лимит). Без счётчика
  напоминание либо терялось бы молча, либо долбило бы одну и ту же ошибку вечно.
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        -- ---------- журнал: вердикт верификатора ----------

        ALTER TABLE governance.decision_records DROP CONSTRAINT decision_records_kind;
        ALTER TABLE governance.decision_records ADD CONSTRAINT decision_records_kind CHECK
            (kind IN ('llm_call', 'tool_run', 'policy', 'turn_summary', 'verdict'));

        -- ---------- рабочее расписание ----------

        CREATE TABLE governance.reminders (
            id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id    bigint      NOT NULL,
            text        text        NOT NULL,
            due_at      timestamptz NOT NULL,
            status      text        NOT NULL DEFAULT 'scheduled',
            trace_id    uuid,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            sent_at     timestamptz,
            attempts    integer     NOT NULL DEFAULT 0,
            last_error  text,
            CONSTRAINT reminders_status CHECK
                (status IN ('scheduled', 'sent', 'cancelled', 'failed')),
            CONSTRAINT reminders_text_len CHECK (char_length(text) BETWEEN 1 AND 2000),
            CONSTRAINT reminders_attempts CHECK (attempts >= 0 AND attempts <= 24)
        );

        -- частичный индекс: tick ищет только то, что ещё должно сработать; за год «sent» в таблице
        -- стали бы основным содержимым, и полный индекс только мешал бы
        CREATE INDEX reminders_due ON governance.reminders (due_at)
            WHERE status = 'scheduled';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS governance.reminders_due;
        DROP TABLE IF EXISTS governance.reminders CASCADE;

        ALTER TABLE governance.decision_records DROP CONSTRAINT decision_records_kind;
        ALTER TABLE governance.decision_records ADD CONSTRAINT decision_records_kind CHECK
            (kind IN ('llm_call', 'tool_run', 'policy', 'turn_summary'));
        """
    )
