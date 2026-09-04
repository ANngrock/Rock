"""Порт записи событий и его адаптеры.

`EventSink` — узкий интерфейс, который используют supervisor и use cases. Адаптеры:

* :class:`OutboxEventSink` — пишет в Postgres (events + outbox одной транзакцией);
* :class:`NullEventSink` — no-op, когда БД недоступна (деградация по принципу «бот живёт без
  всего, кроме owner-команд»);
* :class:`BestEffortEventSink` — оборачивает реальный sink и не даёт ошибке БД убить ответ
  владельцу, но пишет warning (потеря трассы должна быть видна).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import structlog

from aegis.platform.db import session
from aegis.platform.events.store import EventStore

__all__ = [
    "BestEffortEventSink",
    "EventSink",
    "InMemoryEventSink",
    "NullEventSink",
    "OutboxEventSink",
]

log = structlog.get_logger(__name__)


@runtime_checkable
class EventSink(Protocol):
    async def append(
        self,
        *,
        stream_type: str,
        stream_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...


class NullEventSink:
    async def append(
        self,
        *,
        stream_type: str,
        stream_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return None


class InMemoryEventSink:
    """Для тестов: собирает события в список."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append(
        self,
        *,
        stream_type: str,
        stream_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            {
                "stream_type": stream_type,
                "stream_id": stream_id,
                "type": event_type,
                "payload": payload or {},
            }
        )


class OutboxEventSink:
    async def append(
        self,
        *,
        stream_type: str,
        stream_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        async with session() as s:
            await EventStore(s).append(
                stream_type=stream_type,
                stream_id=stream_id,
                event_type=event_type,
                payload=payload,
                metadata=metadata,
            )


class BestEffortEventSink:
    def __init__(self, inner: EventSink) -> None:
        self._inner = inner

    async def append(
        self,
        *,
        stream_type: str,
        stream_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self._inner.append(
                stream_type=stream_type,
                stream_id=stream_id,
                event_type=event_type,
                payload=payload,
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001 - трасса не должна ронять ответ владельцу
            log.warning("event_sink.degraded", stream=stream_id, event=event_type, err=repr(exc))
