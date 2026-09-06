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

from aegis.platform.db import SessionFactory, session

__all__ = ["ConcurrencyError", "EventRecord", "EventStore", "dlq_stats", "replay_from_seq"]


class ConcurrencyError(RuntimeError):
    """Нарушен optimistic concurrency контроль потока."""


class EventRecord(dict[str, Any]):
    """Тонкий dict-псевдоним: payload уже декодирован драйвером."""


_INSERT_EVENT = text(
    """
    INSERT INTO platform.events
        (stream_type, stream_id, version, event_type, payload, metadata, owner_id, schema_version)
    VALUES (:stream_type, :stream_id, :version, :event_type,
            CAST(:payload AS jsonb), CAST(:metadata AS jsonb), :owner_id, :schema_version)
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
        owner_id: int = 0,
        schema_version: int | None = None,
    ) -> int:
        """Записать событие в поток. Возвращает id строки событий.

        ``expected_version`` — оптимистичная блокировка: при расхождении бросает
        :class:`ConcurrencyError`, и вся транзакция (включая доменную запись) откатится.

        ``owner_id`` (F2) — чьё это событие, для RLS; ``schema_version`` (F4) — версия контракта
        payload, фиксируется при рождении: «на какой версии ушло» должно читаться из строки,
        а не выводиться из сегодняшнего реестра схем.
        """
        from aegis.platform.events.contracts import EVENT_REGISTRY  # noqa: PLC0415

        if schema_version is None:
            entry = EVENT_REGISTRY.get(event_type)
            schema_version = int(entry["schema_version"]) if entry else 1
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
                owner_id=int(owner_id),
                schema_version=int(schema_version),
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
               e.event_type, e.payload, e.metadata, o.attempts, e.schema_version
        FROM platform.outbox o
        JOIN platform.events e ON e.id = o.event_id
        WHERE o.published_at IS NULL
          AND o.abandoned_at IS NULL
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
            "attempts": int(r[8] or 0),
            "schema_version": int(r[9] or 1),
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

    async def move_to_dlq(self, outbox_id: int, reason: str, body: dict[str, Any]) -> None:
        """«Ядовитое» событие: копия тела + причина в DLQ, строка очереди помечена снятой.

        Это единственная операция, которая ВЫНОСИТ строку из ротации попыток — и она не «удаляет»:
        событие остаётся в ``platform.event_dlq`` с полным телом, «догнать после починки»
        (:func:`replay_from_seq`) — ровно про эти строки.
        """
        await self._s.execute(
            text(
                "INSERT INTO platform.event_dlq (outbox_id, event_id, reason, body)"
                " SELECT o.id, o.event_id, :reason, CAST(:body AS jsonb)"
                " FROM platform.outbox o WHERE o.id = CAST(:id AS bigint)"
                " ON CONFLICT (outbox_id) DO NOTHING"
            ).bindparams(
                reason=reason[:2000], body=orjson.dumps(body, default=str).decode(), id=outbox_id
            )
        )
        await self._s.execute(
            text(
                "UPDATE platform.outbox SET abandoned_at = now(), abandoned_reason = :reason"
                " WHERE id = CAST(:id AS bigint)"
            ).bindparams(reason=reason[:2000], id=outbox_id)
        )

    async def replay_reset(
        self, *, from_seq: int, to_seq: int | None = None, event_type: str | None = None
    ) -> int:
        """Сбросить отметки доставки событиям окна — «догнать после починки», идемпотентно по id.

        Повторный вызов не создаёт строк: мы не «переиздаём событие» (это был бы второй
        event_id — то есть новый факт), а снимаем отметку с существующей доставки. Потребитель
        по-прежнему дедуплицирует по ``event_id`` (ADR-0013), а ``from_seq`` привязан к
        monotonic id потока — «окно, а не вся история».
        """
        # никаких интерполяций значений — два статических варианта запроса (с правым краем окна
        # и без), SELECT-константы читаются глазами; «f-строка для красоты» здесь была бы дырой
        if to_seq is not None:
            sql = (
                "UPDATE platform.outbox o"
                "   SET published_at = NULL, attempts = 0, last_error = NULL, abandoned_at = NULL"
                "  FROM platform.events e"
                " WHERE o.event_id = e.id AND e.id >= :from_seq AND e.id <= :to_seq"
                "   AND (CAST(:event_type AS text) IS NULL"
                "        OR e.event_type = CAST(:event_type AS text))"
            )
        else:
            sql = (
                "UPDATE platform.outbox o"
                "   SET published_at = NULL, attempts = 0, last_error = NULL, abandoned_at = NULL"
                "  FROM platform.events e"
                " WHERE o.event_id = e.id AND e.id >= :from_seq"
                "   AND (CAST(:event_type AS text) IS NULL"
                "        OR e.event_type = CAST(:event_type AS text))"
            )
        params: dict[str, Any] = {"from_seq": int(from_seq), "event_type": event_type}
        if to_seq is not None:
            params["to_seq"] = int(to_seq)
        result = await self._s.execute(text(sql), params)
        return int(result.rowcount or 0)

    async def dlq_rows(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._s.execute(
            text(
                "SELECT id, outbox_id, event_id, reason, body, dead_at,"
                " replayed_at IS NOT NULL AS replayed"
                " FROM platform.event_dlq ORDER BY id DESC LIMIT :limit"
            ).bindparams(limit=int(limit))
        )
        return [
            {
                "id": r[0],
                "outbox_id": r[1],
                "event_id": r[2],
                "reason": r[3],
                "body": r[4],
                "dead_at": r[5].isoformat(),
                "replayed": bool(r[6]),
            }
            for r in rows
        ]

    async def counts(self, *, max_attempts: int | None = None) -> dict[str, int]:
        """Состояние очереди: сколько ждёт и сколько уже не retry'ится."""
        row = await self._s.execute(
            text(
                """
                -- «stuck» после F4 — это abandoned: вычерпанная из ротации строка требует
                -- реакции человека и больше попыток не жжёт. pending считает только живых
                SELECT count(*)
                           FILTER (WHERE published_at IS NULL AND abandoned_at IS NULL)::int
                           AS pending,
                       count(*) FILTER (WHERE abandoned_at IS NOT NULL)::int AS stuck,
                       count(*) FILTER (WHERE abandoned_at IS NOT NULL)::int AS abandoned
                FROM platform.outbox
                """
            )
        )
        r = row.one()
        return {"pending": int(r[0]), "stuck": int(r[1]), "abandoned": int(r[2] or 0)}


async def dlq_stats(session_factory: SessionFactory | None = None) -> dict[str, int]:
    """Счётчики DLQ для doctor'а: всего, новых (не переигранных), самая свежая причина."""
    sm = session_factory or session
    async with sm() as s:
        row = (
            await s.execute(
                text(
                    "SELECT count(*)::int, count(*) FILTER (WHERE replayed_at IS NULL)::int,"
                    " coalesce(max(reason), '') FROM platform.event_dlq"
                )
            )
        ).one()
    return {
        "total": int(row[0] or 0),
        "unreplayed": int(row[1] or 0),
        "last_reason": str(row[2])[:200],
    }


async def replay_from_seq(
    from_seq: int,
    *,
    to_seq: int | None = None,
    event_type: str | None = None,
    session_factory: SessionFactory | None = None,
) -> int:
    """CLI-тонкость: открыть транзакцию и вызвать :meth:`EventStore.replay_reset`."""
    sm = session_factory or session
    async with sm() as s:
        reset = await EventStore(s).replay_reset(
            from_seq=from_seq, to_seq=to_seq, event_type=event_type
        )
        await s.commit()
    return reset
