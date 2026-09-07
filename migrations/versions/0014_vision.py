"""0014: зрение — сессии live-камеры и лента событий. Хозяин таблиц — TS-сервер мини-аппа.

Python-контур владеет схемой (миграции — единственный источник DDL, и TS-сервер права её менять
не имеет), TS пишет строки. Философия та же, что у узлов и инбокса: кадры — транзитом, факт —
в БД. Raw-изображения не храним нигде: «бот смотрел» — это метаданные и тексты анализа;
картинка с камеры принадлежит устройству владельца, не серверу.

events.kind закрыт CHECK'ом: поток лога без алфавита превращается в свалку, которую не
разобрать запросом. ok/error — потому что «анализ не пришёл» обязан быть различим от «ничего
интересного».
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        SET LOCAL statement_timeout = '30s';
        CREATE SCHEMA IF NOT EXISTS vision;
        CREATE TABLE vision.sessions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tg_user_id bigint NOT NULL,
            started_at timestamptz NOT NULL DEFAULT now(),
            ended_at timestamptz,
            mode text NOT NULL DEFAULT 'stream' CHECK (mode IN ('stream', 'ask', 'brief')),
            frames int NOT NULL DEFAULT 0,
            analyses int NOT NULL DEFAULT 0,
            engine text,
            note text
        );
        CREATE TABLE vision.events (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            session_id uuid NOT NULL REFERENCES vision.sessions(id) ON DELETE CASCADE,
            kind text NOT NULL
                CHECK (kind IN ('frame', 'analysis', 'question', 'answer', 'error', 'note')),
            text text,
            ok boolean NOT NULL DEFAULT true,
            ms int,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX vision_events_session ON vision.events (session_id, id);
        CREATE INDEX vision_sessions_open ON vision.sessions (tg_user_id, started_at DESC)
            WHERE ended_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS vision.events;
        DROP TABLE IF EXISTS vision.sessions;
        DROP SCHEMA IF EXISTS vision;
        """
    )
