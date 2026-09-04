"""Порт записи событий и его адаптеры.

`EventSink` — узкий интерфейс, который используют supervisor и use cases. Адаптеры:

* :class:`OutboxEventSink` — пишет в Postgres (events + outbox одной транзакцией);
* :class:`NullEventSink` — no-op, когда БД недоступна (деградация по принципу «бот живёт без
  всего, кроме owner-команд»);
* :class:`BestEffortEventSink` — оборачивает реальный sink и не даёт ошибке БД убить ответ
  владельцу, но пишет warning (потеря трассы должна быть видна).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
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

# "relation ... does not exist" — типичный след непрокатанных миграций
_MISSING_RELATION = re.compile(r"(relation|table).*(does not exist|undefined)", re.IGNORECASE)


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
    """Не роняет пользовательский ход из-за трассировки, но и не молчит вечно.

    Лог деградации ограничивается по времени: при лёгшем вниз Postgres сообщение о каждом
    событии превратило бы журнал в мусор. После первой же ошибки мы говорим, что именно
    произошло и как это починить, а дальше пишем редко.
    """

    _LOG_EVERY_SECONDS = 30.0

    def __init__(self, inner: EventSink, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._inner = inner
        self._clock = clock
        self._failures = 0
        self._last_logged = 0.0
        self._hinted_missing_schema = False

    @property
    def failures(self) -> int:
        return self._failures

    @property
    def degraded(self) -> bool:
        return self._failures > 0

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
            # ВАЖНО: ключ `event` у structlog занят под текст сообщения — называть его
            # аргументом нельзя (TypeError внутри обработчика отказа = падение всего хода).
            self._failures += 1
            err = repr(exc)
            if not self._hinted_missing_schema and _MISSING_RELATION.search(err):
                self._hinted_missing_schema = True
                log.error(
                    "event_sink.schema_missing",
                    hint="накай миграции: docker compose -f deploy/docker-compose.yml "
                    "run --rm bot alembic upgrade head",
                    err=err[:200],
                )
            now = self._clock()
            if now - self._last_logged >= self._LOG_EVERY_SECONDS:
                self._last_logged = now
                log.warning(
                    "event_sink.degraded",
                    stream=stream_id,
                    event_type=event_type,
                    failures=self._failures,
                    err=err[:300],
                )
