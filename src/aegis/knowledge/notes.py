"""Знания: заметки, сохранённые страницы/файлы (шаг 1) и RAG поверх них.

Поиск — гибрид: вектор по pgvector (если эмбеддинг построен) + детерминированный текстовый
фолбэк (принцип 5: работает всегда, даже при недоступных эмбеддингах). Эмбеддинги строит фоновый
проход `aegis index notes` (`knowledge.index`) — он обязан успевать за потоком сохранённых страниц,
и поиск от его отсутствия только теряет семантику, но не становится бессильным.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text

from aegis.platform.db import SessionFactory, session

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
