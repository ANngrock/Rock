"""0004: планирование как слой — таблица напоминаний уходит из governance в planning.

Зачем отдельная миграция вместо правки 0003: 0003 уже применялась (и к локальной базе, и, как
только будет развёртывание, к боевой), а миграция, которую можно переписать, — это не миграция.

Смысл переезда: ``governance`` — это то, что агента *ограничивает* (политика, kill switch,
tool_runs, журнал решений). Напоминание владельца — не ограничение, а рабочий план слоя
``planning``; в
частности, к нему применим будущий планировщик (Temporal), который тоже живёт в ``planning``.
Ссылка ``trace_id`` остаётся: из строки расписания по-прежнему видно, какой ход её поставил.

Таблица переезжает целиком (``ALTER TABLE ... SET SCHEMA``): данные, индексы, constraints и триггеры
остаются на месте — меняется только схема-владелец.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE SCHEMA IF NOT EXISTS planning;
        """
    )
    # Таблица могла и не создаться (0003 применялась к базе без неё — маловероятно, но проверка
    # дешевле, чем падение миграции на полпути).
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                 WHERE table_schema = 'governance' AND table_name = 'reminders'
            ) THEN
                ALTER TABLE governance.reminders SET SCHEMA planning;
            END IF;
        END
        $$;
        """
    )
    # Аренда строки вместо «посмотрел — отправил»: тик может идти минутами (Telegram лимиты,
    # таймауты), а следующий стартует через пять. Без промежуточного статуса эти два тика
    # соревновались бы в одной и той же строке и прислали владельцу дубликат.
    op.execute(
        """
        ALTER TABLE planning.reminders DROP CONSTRAINT IF EXISTS reminders_status;
        ALTER TABLE planning.reminders ADD CONSTRAINT reminders_status CHECK
            (status IN ('scheduled', 'sending', 'sent', 'cancelled', 'failed'));
        """
    )
    # Частичный индекс 0003 отбирал только 'scheduled'; теперь tick читает и «зависшие» аренды,
    # иначе он каждый раз сканировал бы таблицу целиком
    op.execute(
        """
        DROP INDEX IF EXISTS planning.reminders_due;
        CREATE INDEX reminders_due ON planning.reminders (due_at)
            WHERE status IN ('scheduled', 'sending');
        """
    )
    op.execute(
        """
        COMMENT ON SCHEMA planning IS 'Планы и расписания: напоминания, будущий планировщик';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS planning.reminders_due;
        CREATE INDEX reminders_due ON planning.reminders (due_at) WHERE status = 'scheduled';
        """
    )
    op.execute(
        """
        ALTER TABLE planning.reminders DROP CONSTRAINT IF EXISTS reminders_status;
        ALTER TABLE planning.reminders ADD CONSTRAINT reminders_status CHECK
            (status IN ('scheduled', 'sent', 'cancelled', 'failed'));
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                 WHERE table_schema = 'planning' AND table_name = 'reminders'
            ) THEN
                ALTER TABLE planning.reminders SET SCHEMA governance;
            END IF;
        END
        $$;
        """
    )
