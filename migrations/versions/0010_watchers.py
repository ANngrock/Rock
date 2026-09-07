"""0010: наблюдатели — «следи, пока не случится», поверх расписания напоминаний.

Напоминание знает ОДИН момент; сценарий «помониторь, и когда X — звони» — это повторная проверка
условия. Таблица намеренно не содержит ни текста доставки, ни канала отправки в свой адрес: при
срабатывании наблюдатель СОЗДАЁТ напоминание (тот же planning.reminders, те же каналы message/call/
both, тот же fallback). Два механизма доставки — это два механизма рассинхрона; здесь их нет.

kind: page — состояние URL (содержит/regex/изменилось), search — выдача поиска по запросу.
Режим 'changed' не хранит «что искать»: хранится хэш последнего виденного текста (baseline_hash),
первый проход его только засеивает — иначе «страница изменилась» сработала бы на первой же
проверке. interval с полом в 5 минут: наблюдение чаще — это DDoS чужого сайта нашим таймером.
expires_at опционален: «следи до пятницы» обязан уметь заканчиваться сам, вечные проверки —
это медленная утечка смысла (страница поменялась, условие «случилось», а кому оно нужно —
забылось).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        SET LOCAL statement_timeout = '30s';
        CREATE TABLE planning.watches (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id bigint NOT NULL,
            title text NOT NULL CHECK (length(btrim(title)) >= 3),
            kind text NOT NULL CHECK (kind IN ('page', 'search')),
            target text NOT NULL,
            mode text NOT NULL CHECK (mode IN ('contains', 'regex', 'changed')),
            needle text,
            interval_minutes integer NOT NULL CHECK (interval_minutes BETWEEN 5 AND 14400),
            channel text NOT NULL DEFAULT 'message'
                CHECK (channel IN ('message', 'call', 'both')),
            repeat boolean NOT NULL DEFAULT false,
            status text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'paused', 'fired', 'cancelled', 'expired')),
            fire_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz,
            baseline_hash text,
            last_state text CHECK (last_state IN ('no', 'yes')),
            last_error text,
            failures integer NOT NULL DEFAULT 0,
            trace_id text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT watches_needle_required CHECK (mode = 'changed' OR needle IS NOT NULL)
        );
        CREATE INDEX watches_due ON planning.watches (fire_at) WHERE status = 'active';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        DROP TABLE IF EXISTS planning.watches;
        """
    )
