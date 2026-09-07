"""0013: когнитивный слой — воронка понимания, эмоциональный контур, голос, инбокс чатов.

Три таблицы — три разных провала, которые они закрывают:

* `lexicon`: «жаргон и сокращения» не должны каждый раз угадываться моделью — однажды
  объяснённое владельцем значение становится фактом и живёт в БД (принцип: данные не в контексте);
* `affect_state`: настроение — состояние, а не реплика. Без таблицы «бот разозлился» забывался
  на следующем ходе, а человек не забывает: полураспад задан явно, накопление тоже;
* `voice_log` и `stickers`: распознавание и реакции — проверяемые данные: «что бот услышал»
  и «какую наклейку послал» должны читаться постфактум, иначе «он меня не так понял»
  неразрешимо.

`chat_policies`/`inbox` — пользовательский аккаунт как источник событий: бот в личные чаты
не ходит (Telegram не умеет), туда ходит userbot-демон владельца; сюда он кладёт входящие,
сюда же падают вердикты воронки и тексты черновиков — каждый автоответ оставляет строку.
``auto`` без строки в inbox невозможен физически: нет записи — нет отправки.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        SET LOCAL statement_timeout = '30s';
        CREATE SCHEMA IF NOT EXISTS cognition;
        CREATE TABLE cognition.lexicon (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            owner_id bigint NOT NULL,
            term text NOT NULL CHECK (length(term) BETWEEN 1 AND 64),
            means text NOT NULL CHECK (length(means) BETWEEN 1 AND 500),
            kind text NOT NULL DEFAULT 'term' CHECK (kind IN ('term', 'person', 'style')),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT lexicon_one_term_per_owner UNIQUE (owner_id, term)
        );
        CREATE TABLE cognition.affect_state (
            owner_id bigint PRIMARY KEY,
            valence real NOT NULL DEFAULT 0 CHECK (valence BETWEEN -1 AND 1),
            arousal real NOT NULL DEFAULT 0.3 CHECK (arousal BETWEEN 0 AND 1),
            frustration real NOT NULL DEFAULT 0 CHECK (frustration BETWEEN 0 AND 1),
            mood text NOT NULL DEFAULT 'neutral',
            turns int NOT NULL DEFAULT 0,
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE cognition.voice_log (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id bigint NOT NULL,
            direction text NOT NULL CHECK (direction IN ('in', 'out')),
            engine text NOT NULL,
            text text,
            seconds int,
            ok boolean NOT NULL DEFAULT true,
            error text,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX voice_log_recent ON cognition.voice_log (owner_id, created_at DESC);
        CREATE TABLE cognition.stickers (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            owner_id bigint NOT NULL,
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 40),
            file_id text NOT NULL,
            moods text[] NOT NULL DEFAULT '{}',
            enabled boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT sticker_one_name_per_owner UNIQUE (owner_id, name)
        );
        CREATE TABLE cognition.chat_policies (
            owner_id bigint NOT NULL,
            peer text NOT NULL CHECK (length(peer) BETWEEN 1 AND 128),
            mode text NOT NULL DEFAULT 'watch'
                CHECK (mode IN ('off', 'watch', 'draft', 'auto')),
            note text,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (owner_id, peer)
        );
        CREATE TABLE cognition.inbox (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id bigint NOT NULL,
            daemon text NOT NULL,
            chat_id text NOT NULL,
            chat_name text,
            from_name text,
            text text NOT NULL,
            msg_uid text NOT NULL,
            verdict text NOT NULL DEFAULT 'new'
                CHECK (verdict IN ('new', 'noise', 'info', 'important', 'action_required',
                                   'urgent')),
            reason text,
            reply text,
            status text NOT NULL DEFAULT 'stored'
                CHECK (status IN ('stored', 'notified', 'draft', 'sent', 'discarded',
                                  'blocked')),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT inbox_one_message_per_owner UNIQUE (owner_id, chat_id, msg_uid)
        );
        CREATE INDEX inbox_open ON cognition.inbox (owner_id, created_at DESC)
            WHERE status IN ('stored', 'notified', 'draft');
        CREATE TABLE cognition.userbots (
            daemon text PRIMARY KEY,
            owner_id bigint NOT NULL,
            last_seen timestamptz NOT NULL DEFAULT now(),
            caps jsonb NOT NULL DEFAULT '{}'::jsonb
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS cognition.userbots;
        DROP TABLE IF EXISTS cognition.inbox;
        DROP TABLE IF EXISTS cognition.chat_policies;
        DROP TABLE IF EXISTS cognition.stickers;
        DROP TABLE IF EXISTS cognition.voice_log;
        DROP TABLE IF EXISTS cognition.affect_state;
        DROP TABLE IF EXISTS cognition.lexicon;
        DROP SCHEMA IF EXISTS cognition;
        """
    )
