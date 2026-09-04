"""Долговременные факты о владельце.

Данные живут здесь, а не «в контексте» (принцип 1): системный промпт собирается из БД на каждом
ходе, поэтому история диалога — лишь рабочий кэш, а истина — в таблице ``memory.facts``.

Facts — это утверждения о человеке в 3-м лице («не пьёт кофе после 16:00»), а не заметки и не
журнал событий. ``valid_to`` даёт мягкое вытеснение/отмену (нужно для ``/forget`` на шаге 6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text

from aegis.platform.db import SessionFactory, session

__all__ = ["Fact", "Facts", "FactsRepo"]

CATEGORIES = ("general", "preferences", "work", "health", "finance", "people", "home")


@dataclass(slots=True)
class Fact:
    id: str
    fact: str
    category: str
    importance: float


class Facts(Protocol):
    """Порт для инструментов/супервизора: репозиторий и in-memory заглушка взаимозаменяемы."""

    async def add(
        self, fact: str, category: str = "general", *, source: str, importance: float = 0.5
    ) -> str: ...

    async def recent(self, limit: int = 50) -> list[str]: ...

    async def list(self, limit: int = 50) -> list[Fact]: ...

    async def invalidate(self, fact_id: str) -> bool: ...


class FactsRepo:
    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._sm = session_factory

    async def add(
        self,
        fact: str,
        category: str = "general",
        *,
        source: str = "owner",
        importance: float = 0.5,
    ) -> str:
        sm = self._sm or session
        async with sm() as s:
            row_id = await s.scalar(
                text(
                    """
                    INSERT INTO memory.facts (fact, category, source, importance)
                    VALUES (:fact, :category, :source, :importance)
                    ON CONFLICT (fact) DO UPDATE
                    SET importance = GREATEST(memory.facts.importance, EXCLUDED.importance),
                        -- «вспомнил заново» возвращает мягко удалённый факт в контекст
                        valid_to = NULL,
                        updated_at = now()
                    RETURNING id::text
                    """
                ).bindparams(
                    fact=fact.strip(), category=category, source=source, importance=importance
                )
            )
            return str(row_id)

    async def recent(self, limit: int = 50) -> list[str]:
        return [f.fact for f in await self.list(limit)]

    async def list(self, limit: int = 50) -> list[Fact]:
        sm = self._sm or session
        async with sm() as s:
            rows = await s.execute(
                text(
                    """
                    SELECT id::text, fact, category, importance
                    FROM memory.facts
                    WHERE valid_to IS NULL
                    ORDER BY importance DESC, created_at DESC
                    LIMIT :limit
                    """
                ).bindparams(limit=limit)
            )
            return [Fact(id=r[0], fact=r[1], category=r[2], importance=float(r[3])) for r in rows]

    async def invalidate(self, fact_id: str) -> bool:
        """Мягкое удаление: факт перестаёт попадать в контекст, но остаётся в истории."""
        sm = self._sm or session
        async with sm() as s:
            row = await s.execute(
                text(
                    "UPDATE memory.facts SET valid_to = now(), updated_at = now() "
                    "WHERE id = CAST(:id AS uuid) AND valid_to IS NULL RETURNING id"
                ).bindparams(id=fact_id)
            )
            return row.first() is not None
