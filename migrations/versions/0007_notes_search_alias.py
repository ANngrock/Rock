"""0007: алиас поискового индекса заметок (F9): перестройка — flip, а не переигрывание.

``index_version`` на строке + ``knowledge.index_state`` + вьюха ``knowledge.notes_search``:
поиск читает вьюху, вьюха показывает строки «текущей» версии. Перестройка индекса =
пакетный проход поднимает у строк index_version до N+1, «применение» — один UPDATE строки
состояния (атомарно, в пределах view switch), откат — UPDATE обратно. Никаких ALTER на
живой таблице поиска и никакого TRUNCATE/RECREATE с окном «поиск молчит»: переиндексация
перестаёт быть событием релиза и становится операцией.

TRGM-индексы — CONCURRENTLY и вне транзакции (F10: ``op.get_context().autocommit_block()``):
обычный CREATE INDEX на живой notes taking ACCESS EXCLUSIVE на секунды сканирования — а
заметки пишут и во время переиндексации. IF NOT EXISTS + IF NOT EXISTS по имени: повторный
проход миграции после обрыва не оставляет INVALID-индекс молча, но и не падает — инвалидность
чинит `aegis migrate status` (он её видит и говорит «пересоздай»).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        SET LOCAL statement_timeout = '30s';
        -- expand-шаг: только ADD COLUMN с DEFAULT (metadatum-операция с PG11) и таблицы
        -- состояния. Никаких drop в этом релизе — contract-часть уйдёт следующим (F10)
        ALTER TABLE knowledge.notes
            ADD COLUMN IF NOT EXISTS index_version integer NOT NULL DEFAULT 1;
        CREATE TABLE IF NOT EXISTS knowledge.index_state (
            name        text        PRIMARY KEY,
            version     integer     NOT NULL,
            note        text        NOT NULL DEFAULT '',
            switched_at timestamptz NOT NULL DEFAULT now()
        );
        INSERT INTO knowledge.index_state (name, version, note)
            VALUES ('notes', 1, 'baseline: алиас введён, перестроек ещё не было')
            ON CONFLICT (name) DO NOTHING;
        """
    )
    op.execute(
        """
        -- алиас: поиск ходит ТОЛЬКО через эту вьюху (в коде репозитория — см. notes.py);
        -- она же и есть «активная версия индекса»: flip = UPDATE index_state.version
        CREATE OR REPLACE VIEW knowledge.notes_search AS
        SELECT n.id, n.title, n.body, n.tags, n.embedding, n.created_at, n.index_version
        FROM knowledge.notes n
        JOIN knowledge.index_state st ON st.name = 'notes' AND n.index_version = st.version;
        COMMENT ON VIEW knowledge.notes_search IS
            'active search alias (F9): rows of the current index_version only; rollback = flip';
        """
    )
    # trgm для «дешёвого лексического» слоя гибрида (F9). CONCURRENTLY нельзя в транзакции —
    # отдельный autocommit-блок, как требует контракт zero-downtime (F10)
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS notes_title_trgm_v7_idx ON knowledge.notes USING gin (title gin_trgm_ops);
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS notes_body_trgm_v7_idx ON knowledge.notes USING gin (body gin_trgm_ops);
            """
        )
    op.execute(
        """
        -- индекс под «кого переиндексировать»: backfill выбирает пачку строк со СТАРОЙ
        -- версией, и «список устаревших» не должен каждый раз перечитывать всю таблицу
        CREATE INDEX IF NOT EXISTS notes_index_version_idx
            ON knowledge.notes (index_version);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        SET LOCAL lock_timeout = '5s';
        DROP VIEW IF EXISTS knowledge.notes_search;
        DROP INDEX IF EXISTS notes_index_version_idx;
        -- колонку и индекс-состояние НЕ удаляем: contract-шаг (drop) — отдельный релиз,
        -- иначе откат после первых же переиндексированных строк уничтожит данные
        """
    )
