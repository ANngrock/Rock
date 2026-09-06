"""Знания: заметки, сохранённые страницы/файлы (шаг 1) и RAG поверх них.

Поиск — гибрид: вектор по pgvector (если эмбеддинг построен) + детерминированный текстовый
фолбэк (принцип 5: работает всегда, даже при недоступных эмбеддингах). Эмбеддинги строит фоновый
проход `aegis index notes` (`knowledge.index`) — он обязан успевать за потоком сохранённых страниц,
и поиск от его отсутствия только теряет семантику, но не становится бессильным.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from sqlalchemy import text

from aegis.knowledge.ranking import Doc, LexicalReranker, fuse_rrf
from aegis.platform.db import SessionFactory, session

log = structlog.get_logger(__name__)

__all__ = ["Note", "NoteHit", "Notes", "NotesRepo"]

_VECTOR_SEARCH = text(
    """
    SELECT id::text,
           title,
           body,
           1 - (embedding <=> CAST(:embedding AS vector)) AS score,
           'vector' AS method
    FROM knowledge.notes
    WHERE embedding IS NOT NULL
    -- created_at в конец: при равных расстояниях без тай-брейка порядок произвольный,
    -- и «что нашлось» начинает зависеть от плана запроса (это ловилось на живом Postgres)
    ORDER BY embedding <=> CAST(:embedding AS vector), created_at DESC
    LIMIT :limit
    """
)

_TEXT_SEARCH = text(
    """
    SELECT id::text,
           title,
           body,
           ts_rank(to_tsvector('simple', title || ' ' || body),
                   websearch_to_tsquery('simple', :query)) AS score,
           'text' AS method
    FROM knowledge.notes
    WHERE to_tsvector('simple', title || ' ' || body) @@ websearch_to_tsquery('simple', :query)
    ORDER BY score DESC, created_at DESC
    LIMIT :limit
    """
)

# ----------------------------------------------------------------- F9: алиас индекса --
# Поиск ходит через вьюху knowledge.notes_search, когда она есть (миграция 0007): строки
# «текущей» версии индекса. Нет вьюхи (база до 0007) — читаем таблицу напрямую: гибрид
# обязан деградировать в «как раньше», а не падать; «индекс перестраивается» не может
# означать «бот оглох».

_VECTOR_SEARCH_V = text(
    """
    SELECT id::text,
           title,
           body,
           1 - (embedding <=> CAST(:embedding AS vector)) AS score,
           'vector' AS method
    FROM knowledge.notes_search
    WHERE embedding IS NOT NULL
    ORDER BY embedding <=> CAST(:embedding AS vector), created_at DESC
    LIMIT :limit
    """
)

_TEXT_SEARCH_V = text(
    """
    SELECT id::text,
           title,
           body,
           ts_rank(to_tsvector('simple', title || ' ' || body),
                   websearch_to_tsquery('simple', :query)) AS score,
           'text' AS method
    FROM knowledge.notes_search
    WHERE to_tsvector('simple', title || ' ' || body) @@ websearch_to_tsquery('simple', :query)
    ORDER BY score DESC, created_at DESC
    LIMIT :limit
    """
)

_HAS_ALIAS = text("SELECT to_regclass('knowledge.notes_search') IS NOT NULL")

_LIKE_SEARCH = text(
    """
    SELECT id::text, title, body, 0.5::real AS score, 'like' AS method
    FROM knowledge.notes
    WHERE title ILIKE :needle OR body ILIKE :needle
    ORDER BY created_at DESC
    LIMIT :limit
    """
)


@dataclass(slots=True)
class Note:
    id: str
    title: str
    body: str
    tags: list[str]


@dataclass(slots=True)
class NoteHit:
    id: str
    title: str
    body: str
    score: float
    method: str


class IndexState(Protocol):
    """Контракт «какая версия индекса активна» — для CLI, doctor'а и backfill-прохода."""

    async def alias_version(self) -> int | None: ...

    async def flip_alias(self, version: int, *, note: str = "") -> bool: ...


class Notes(Protocol):
    async def add(
        self, title: str, body: str = "", tags: list[str] | None = None, *, source: str = "owner"
    ) -> Note: ...

    async def search(
        self, query: str, embedding: list[float] | None = None, limit: int = 5
    ) -> list[NoteHit]: ...


class NotesRepo:
    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._sm = session_factory
        # «есть ли алиас» читаем один раз на репозиторий: to_regclass дёшев, но на каждый
        # search — это лишний RTT в самом горячем пути ассистента
        self._alias_ok: bool | None = None

    async def add(
        self,
        title: str,
        body: str = "",
        tags: list[str] | None = None,
        *,
        source: str = "owner",
        raw_input: str | None = None,
    ) -> Note:
        sm = self._sm or session
        clean_tags = [t.strip().lower() for t in (tags or []) if t.strip()][:12]
        async with sm() as s:
            row = await s.execute(
                text(
                    """
                    INSERT INTO knowledge.notes (title, body, tags, source, raw_input)
                    VALUES (:title, :body, CAST(:tags AS text[]), :source, :raw_input)
                    RETURNING id::text, title, body, tags
                    """
                ).bindparams(
                    title=title.strip()[:300],
                    body=body,
                    tags=clean_tags,
                    source=source,
                    raw_input=raw_input,
                )
            )
            r = row.one()
            return Note(id=r[0], title=r[1], body=r[2], tags=list(r[3]))

    async def search(
        self, query: str, embedding: list[float] | None = None, limit: int = 5
    ) -> list[NoteHit]:
        sm = self._sm or session
        async with sm() as s:
            hits: list[NoteHit] = []
            if embedding:
                hits = _to_hits(
                    await s.execute(
                        _VECTOR_SEARCH.bindparams(embedding=str(embedding), limit=limit)
                    )
                )
                hits = [h for h in hits if h.score >= 0.2]  # шум отсечь: иначе «найдено» из ничего
            if not hits and query.strip():
                hits = _to_hits(await s.execute(_TEXT_SEARCH.bindparams(query=query, limit=limit)))
            if not hits and query.strip():
                needle = f"%{'%'.join(query.split()[:3])}%"
                hits = _to_hits(
                    await s.execute(_LIKE_SEARCH.bindparams(needle=needle, limit=limit))
                )
            return hits

    async def _alias_available(self) -> bool:
        if self._alias_ok is not None:
            return self._alias_ok
        sm = self._sm or session
        try:
            async with sm() as s:
                self._alias_ok = bool(await s.scalar(_HAS_ALIAS))
        except Exception as exc:  # noqa: BLE001 - недоступна база = «алиаса не видно»
            log.warning("notes.alias_probe_failed", err=repr(exc)[:160])
            self._alias_ok = False
        return self._alias_ok

    async def search_hybrid(
        self,
        query: str,
        embedding: list[float] | None = None,
        limit: int = 5,
        *,
        pool: int | None = None,
    ) -> list[NoteHit]:
        """Гибрид (F9): текстовый и векторный ранжеры, RRF-слияние, дешёвый rerank вершин.

        Порядок, а не скоры, — единица решения: ts_rank и косинус несопоставимы по шкале,
        RRF сравнивает только места. Порог 0.2 для вектора сохранён и здесь: «семантически
        рядом» без сходства — шум, каким бы красивым ни было слияние.
        """
        if not await self._alias_available():
            return await self.search(query, embedding, limit)
        pool = pool or max(limit * 4, 20)
        sm = self._sm or session
        try:
            async with sm() as s:
                text_rows: Any = (
                    await s.execute(_TEXT_SEARCH_V.bindparams(query=query, limit=pool))
                    if query.strip()
                    else []
                )
                vec_rows: Any = (
                    await s.execute(
                        _VECTOR_SEARCH_V.bindparams(embedding=str(embedding), limit=pool)
                    )
                    if embedding
                    else []
                )
        except Exception as exc:  # noqa: BLE001 - сбой вьюхи = откат на прямой путь, не тишина
            log.warning("notes.hybrid_degraded", err=repr(exc)[:200])
            return await self.search(query, embedding, limit)
        rows: dict[str, tuple[Any, ...]] = {}
        text_ids: list[str] = []
        for r in text_rows:
            rows[r[0]] = r
            text_ids.append(r[0])
        vec_ids: list[str] = []
        for r in vec_rows:
            score = float(r[3])
            if score < 0.2:
                continue
            rows.setdefault(r[0], r)
            vec_ids.append(r[0])
        if not text_ids and not vec_ids:
            return await self.search(query, embedding, limit)
        fused = fuse_rrf([text_ids, vec_ids])
        top_ids = [doc_id for doc_id, _ in fused[:pool]]
        docs = [Doc(doc_id=i, title=str(rows[i][1]), body=str(rows[i][2])[:600]) for i in top_ids]
        order = LexicalReranker().rerank(query, docs)
        fused_score = dict(fused)
        hits: list[NoteHit] = []
        for doc_id in order[:limit]:
            r = rows[doc_id]
            base = max((float(r[3]),), default=0.0)
            hits.append(
                NoteHit(
                    id=doc_id,
                    title=str(r[1]),
                    body=str(r[2]),
                    score=round(base * (1.0 + 0.1 * fused_score.get(doc_id, 0.0) * pool), 4),
                    method="hybrid",
                )
            )
        return hits or await self.search(query, embedding, limit)

    async def alias_version(self) -> int | None:
        """Активная версия алиаса (None — если состояния ещё нет в базе)."""
        sm = self._sm or session
        async with sm() as s:
            try:
                raw = await s.scalar(
                    text("SELECT version FROM knowledge.index_state WHERE name = 'notes'")
                )
            except Exception:  # noqa: BLE001 - до миграции «нет состояния» — факт, не авария
                return None
            return int(raw) if raw is not None else None

    async def flip_alias(self, version: int, *, note: str = "") -> bool:
        """Сменить активную версию атомарно (UPDATE одной строки). Rollback = вызов со старой.

        Никакого «подожди, перестраивается»: видимость переключается мгновенно для всех,
        кто читает через алиас; строки старой версии никуда не деваются — они перестают
        попадать в вьюху, и это единственное допустимое «удаление» из индекса.
        """
        sm = self._sm or session
        async with sm() as s:
            try:
                updated = await s.execute(
                    text(
                        "UPDATE knowledge.index_state"
                        " SET version = :v, note = :n, switched_at = now() WHERE name = 'notes'"
                    ).bindparams(v=int(version), n=note[:400])
                )
                await s.commit()
            except Exception as exc:  # noqa: BLE001 - flip не имеет права быть полу-состоянием
                log.warning("notes.alias_flip_failed", err=repr(exc)[:200])
                return False
            return bool(getattr(updated, "rowcount", 0))

    async def notes_older_than_alias(self, limit: int = 200) -> list[Note]:
        """Пачка «кого переиндексировать под текущий алиас» (работает поверх lease-backfill)."""
        sm = self._sm or session
        async with sm() as s:
            try:
                rows = await s.execute(
                    text(
                        """
                        SELECT n.id::text, n.title, n.body, n.tags
                        FROM knowledge.notes n
                        JOIN knowledge.index_state st ON st.name = 'notes'
                        WHERE n.index_version < st.version
                        ORDER BY n.created_at
                        LIMIT :limit
                        """
                    ).bindparams(limit=limit)
                )
            except Exception:  # noqa: BLE001 - нет alias-слоя = нечего и переиндексировать
                return []
            return [Note(id=r[0], title=r[1], body=r[2], tags=list(r[3])) for r in rows]

    async def mark_indexed(self, note_id: str, version: int) -> None:
        sm = self._sm or session
        async with sm() as s:
            await s.execute(
                text(
                    "UPDATE knowledge.notes SET index_version = :v WHERE id = CAST(:id AS uuid)"
                ).bindparams(v=int(version), id=note_id)
            )
            await s.commit()

    async def get(self, note_id: str) -> Note | None:
        sm = self._sm or session
        async with sm() as s:
            row = await s.execute(
                text(
                    "SELECT id::text, title, body, tags FROM knowledge.notes "
                    "WHERE id = CAST(:id AS uuid)"
                ).bindparams(id=note_id)
            )
            r = row.first()
            return Note(id=r[0], title=r[1], body=r[2], tags=list(r[3])) if r else None

    async def count_pending(self) -> int:
        """Сколько заметок ждут эмбеддинга — для `/status`, doctor'а и отчёта индексатора."""
        sm = self._sm or session
        async with sm() as s:
            return int(
                await s.scalar(text("SELECT count(*) FROM knowledge.notes WHERE embedding IS NULL"))
                or 0
            )

    async def pending_embeddings(self, limit: int = 50) -> list[Note]:
        """Для индексатора (`knowledge.index`): заметки без эмбеддинга, старые первыми."""
        sm = self._sm or session
        async with sm() as s:
            rows = await s.execute(
                text(
                    """
                    SELECT id::text, title, body, tags FROM knowledge.notes
                    WHERE embedding IS NULL ORDER BY created_at LIMIT :limit
                    """
                ).bindparams(limit=limit)
            )
            return [Note(id=r[0], title=r[1], body=r[2], tags=list(r[3])) for r in rows]

    async def set_embedding(self, note_id: str, embedding: list[float]) -> None:
        # Колонка vector(N) — фиксированная: проверить размерность здесь дешевле, чем
        # получить ошибку драйвера изнутри индексатора (и непонятно, чья это вина).
        from aegis.platform.config import settings

        expected = settings().embedding_dims
        if len(embedding) != expected:
            raise ValueError(
                f"эмбеддинг для заметки должен быть {expected}-мерным, получено {len(embedding)} "
                f"— сверь embedding_dims с типом колонки в миграции"
            )
        sm = self._sm or session
        async with sm() as s:
            await s.execute(
                text(
                    "UPDATE knowledge.notes SET embedding = CAST(:e AS vector), indexed_at = now() "
                    "WHERE id = CAST(:id AS uuid)"
                ).bindparams(e=str(embedding), id=note_id)
            )


def _to_hits(result: object) -> list[NoteHit]:
    return [
        NoteHit(id=r[0], title=r[1], body=r[2], score=float(r[3]), method=r[4])
        for r in result  # type: ignore[attr-defined]
    ]
