"""0008: drain-очередь получает честное состояние «claimed» (F1, фикс приёмки 200 ходов).

``_POP_NEXT`` писал ``status='running'`` и ``claimed_at`` — ни того статуса, ни колонки в
DDL 0005: очередь «один ход на владельца» на живой базе падала ProgrammingError, то есть
второе сообщение не терялось — терялся весь drain. Expand-шаг: ``claimed_at`` добавляется
(это лизинг «кто и когда забрал», без него перебор «осиротевших» превращается в двойную
доставку живым потребителям), CHECK расширяется до ``('queued','claimed','done','failed')``.

Downgrade без потерь: незавершённые ``claimed`` возвращаются в ``queued`` — «не съедено»
важнее «не забыто, в каком именно состоянии был обрыв».
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        SET LOCAL statement_timeout = '30s';
        DO $$
        DECLARE c record;
        BEGIN
            FOR c IN
                SELECT conname FROM pg_constraint
                 WHERE conrelid = 'governance.turn_queue'::regclass
                   AND contype = 'c' AND conname LIKE '%status%'
            LOOP
                EXECUTE format(
                    'ALTER TABLE governance.turn_queue DROP CONSTRAINT %I', c.conname
                );
            END LOOP;
        END $$;
        ALTER TABLE governance.turn_queue
            ADD CONSTRAINT turn_queue_status_chk
            CHECK (status IN ('queued', 'claimed', 'done', 'failed'));
        ALTER TABLE governance.turn_queue
            ADD COLUMN IF NOT EXISTS claimed_at timestamptz;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        SET LOCAL statement_timeout = '30s';
        UPDATE governance.turn_queue SET status = 'queued' WHERE status = 'claimed';
        ALTER TABLE governance.turn_queue DROP COLUMN IF EXISTS claimed_at;
        ALTER TABLE governance.turn_queue DROP CONSTRAINT IF EXISTS turn_queue_status_chk;
        ALTER TABLE governance.turn_queue
            ADD CONSTRAINT turn_queue_status_chk
            CHECK (status IN ('queued', 'done', 'failed'));
        """
    )
