"""Append-only event store + transactional outbox (ADR-004).

События — источник истины для воспроизводимости (принцип «всё в трассе»): диалог, решения
policy, результаты инструментов. Атомарность с доменной записью обеспечивается тем, что
``append`` выполняется в той же транзакции, что и изменение состояния (один session).

Outbox — мост в NATS JetStream: relay (шаг 2) выбирает строки с ``published_at IS NULL``
и помечает их опубликованными в той же схеме «at-least-once + дедупликация по event_id».
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import orjson
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["ConcurrencyError", "EventRecord", "EventStore"]


class ConcurrencyError(RuntimeError):
    """Нарушен optimistic concurrency контроль потока."""


class EventRecord(dict[str, Any]):
    """Тонкий dict-псевдоним: payload уже декодирован драйвером."""


_INSERT_EVENT = text(
    """
    INSERT INTO platform.events (stream_type, stream_id, version, event_type, payload, metadata)
    VALUES (:stream_type, :stream_id, :version, :event_type,
            CAST(:payload AS jsonb), CAST(:metadata AS jsonb))
    RETURNING id
    """
)

_MAX_VERSION = text(
    "SELECT COALESCE(MAX(version), 0) FROM platform.events WHERE stream_id = :stream_id"
)


class EventStore:
    def __init__(self, s: AsyncSession) -> None:
        self._s = s

    async def append(
        self,
        *,
        stream_type: str,
        stream_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> int:
        """Записать событие в поток. Возвращает id строки событий.

        ``expected_version`` — оптимистичная блокировка: при расхождении бросает
        :class:`ConcurrencyError`, и вся транзакция (включая доменную запись) откатится.
        """
        current = int(await self._s.scalar(_MAX_VERSION.bindparams(stream_id=stream_id)) or 0)
        if expected_version is not None and current != expected_version:
            raise ConcurrencyError(
                f"поток {stream_id}: версия {current} != ожидаемая {expected_version}"
            )

        event_meta: dict[str, Any] = {
            "event_id": str(uuid.uuid4()),
            "causation_id": (metadata or {}).get("causation_id"),
            "occurred_at": datetime.now(UTC).isoformat(),
        } | (metadata or {})
        row_id = await self._s.scalar(
            _INSERT_EVENT.bindparams(
                stream_type=stream_type,
                stream_id=stream_id,
                version=current + 1,
                event_type=event_type,
                payload=orjson.dumps(payload or {}).decode(),
                metadata=orjson.dumps(event_meta).decode(),
            )
        )
        await self._s.execute(
            text("INSERT INTO platform.outbox (event_id) VALUES (:event_id)").bindparams(
                event_id=row_id
            )
        )
        return int(row_id)

    async def load(self, stream_id: str, *, since_version: int = 0) -> list[EventRecord]:
        rows = await self._s.execute(
            text(
                """
                SELECT version, event_type, payload, metadata, occurred_at
                FROM platform.events
                WHERE stream_id = :stream_id AND version > :since_version
                ORDER BY version
                """
            ).bindparams(stream_id=stream_id, since_version=since_version)
        )
        return [
            EventRecord(
                version=r[0],
                type=r[1],
                payload=r[2],
                metadata=r[3],
                occurred_at=r[4].isoformat(),
            )
            for r in rows
        ]

    async def version(self, stream_id: str) -> int:
        return int(await self._s.scalar(_MAX_VERSION.bindparams(stream_id=stream_id)) or 0)

    # --- outbox (для relay'я; публикуется отдельным процессом с шага 2) ---

    _FIELDS = """
        SELECT o.id, o.event_id, e.stream_type, e.stream_id, e.version,
               e.event_type, e.payload, e.metadata
        FROM platform.outbox o
        JOIN platform.events e ON e.id = o.event_id
        WHERE o.published_at IS NULL
          AND (CAST(:max_attempts AS int) IS NULL OR o.attempts < CAST(:max_attempts AS int))
        ORDER BY o.id
        LIMIT :limit
    """

    async def fetch_unpublished(
        self, limit: int = 100, *, max_attempts: int | None = None
    ) -> list[dict[str, Any]]:
        """Строки для публикации. `FOR UPDATE SKIP LOCKED` — чтобы два relay'я не дублировали.

        `max_attempts` отсекает «ядовитые» строки на уровне выборки: событие, которое сервер
        принципиально не принимает, не должно крутиться в каждом тике и вытеснять нормальные.
        """
        rows = await self._s.execute(
            text(self._FIELDS + "FOR UPDATE OF o SKIP LOCKED").bindparams(
                limit=limit, max_attempts=max_attempts
            )
        )
        return [self._row(r) for r in rows]

    async def peek_unpublished(
        self, limit: int = 100, *, max_attempts: int | None = None
    ) -> list[dict[str, Any]]:
        """То же без блокировок и без права что-либо менять — для `--dry-run`."""
        rows = await self._s.execute(
            text(self._FIELDS).bindparams(limit=limit, max_attempts=max_attempts)
        )
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(r: Any) -> dict[str, Any]:
        return {
            "outbox_id": r[0],
            "event_id": r[1],
            "stream_type": r[2],
            "stream_id": r[3],
            "version": r[4],
            "event_type": r[5],
            "payload": r[6],
            "metadata": r[7],
        }

    async def mark_published(self, outbox_ids: Sequence[int]) -> None:
        """Отметить доставленным. Только после ack от сервера — иначе «доставлено» врёт."""
        if not outbox_ids:
            return
        await self._s.execute(
            text(
                "UPDATE platform.outbox SET published_at = now(), last_error = NULL "
                "WHERE id = ANY(CAST(:ids AS bigint[]))"
            ).bindparams(ids=list(outbox_ids))
        )

    async def mark_failed(self, outbox_id: int, error: str) -> None:
        """Неудача: +1 попытка и причина. Строка остаётся в очереди, пока попытки не кончатся."""
        await self._s.execute(
            text(
                "UPDATE platform.outbox SET attempts = attempts + 1, last_error = :err "
                "WHERE id = CAST(:id AS bigint)"
            ).bindparams(err=error[:2000], id=outbox_id)
        )

    async def counts(self, *, max_attempts: int | None = None) -> dict[str, int]:
        """Состояние очереди: сколько ждёт и сколько уже не retry'ится."""
        row = await self._s.execute(
            text(
                """
                SELECT count(*) FILTER (WHERE published_at IS NULL)::int AS pending,
                       count(*) FILTER (
                           WHERE published_at IS NULL AND attempts >= t.threshold
                       )::int AS stuck
                FROM platform.outbox
                CROSS JOIN (
                    SELECT COALESCE(CAST(:max_attempts AS int), 2147483647) AS threshold
                ) t
                """
            ).bindparams(max_attempts=max_attempts)
        )
        r = row.one()
        return {"pending": int(r[0]), "stuck": int(r[1])}
