"""0009: канал доставки напоминаний (message | call | both).

«Напомни текстом» и «позвони» — разные обещания, и разница обязана жить в строке расписания, а
не выводиться из текста при доставке (эвристика «в тексте есть слово *позвони*» — это гадание на
промпт-инъекцию: текст напоминания пишет не только владелец). Expand-миграция: одна колонка с
DEFAULT — старые строки остаются «сообщением», CHECK запрещает всё, чего диспетчер не понимает.

Downgrade — contract-шаг без потерь: колонка удаляется, звонившие раньше строки продолжают
доставляться текстом ровно как до этой функции.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        SET LOCAL statement_timeout = '30s';
        ALTER TABLE planning.reminders
            ADD COLUMN IF NOT EXISTS channel text NOT NULL DEFAULT 'message'
            CONSTRAINT reminders_channel_chk
            CHECK (channel IN ('message', 'call', 'both'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        SET LOCAL statement_timeout = '30s';
        ALTER TABLE planning.reminders DROP COLUMN IF EXISTS channel;
        """
    )
