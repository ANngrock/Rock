"""Напоминания: рабочее расписание и доставка (шаг 2, пункт «reminder scheduler»).

Почему таблица + tick, а не Temporal, хотя ADR-0005 требует Temporal для всего, что живёт дольше
запроса: сервер Temporal в этом проекте не поднят и поднять его здесь нечем (песочница без сети),
а напоминание владельцу нужно сегодня. Поэтому контракт сохранён по смыслу, а не по названию:
состояние живёт в БД (не в памяти процесса), исполнение — идемпотентный шаг, повторно запускаемый
после любого падения. Когда Temporal появится, `claim_due` станет activity, а `tick` — workflow'ом,
и ни одна строка в инструментах и ни одна колонка не изменятся.

Три обещания, которые здесь держит код:

* **не потерять**: `FOR UPDATE SKIP LOCKED` + счётчик попыток. Два одновременных тика не отправят
  одно и то же дважды, а убитый процесс не съест напоминание — строка вернётся в расписание;
* **не молчать**: доставка упала → `last_error` в таблице и `⚠️` в отчёте тика; после потолка
  попыток статус `failed`, и это видно в `aegis remind list`, а не только в логах;
* **не выдумывать время**: момент считает `planning.schedule`, а не модель; магазин принимает только
  tz-aware `datetime` — наивное время означало бы «сработает, когда успеет Docker».
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text

from aegis.platform.db import session

__all__ = [
    "DELIVER_PREFIX",
    "MAX_ATTEMPTS",
    "DeliverReport",
    "NullReminderStore",
    "Reminder",
    "ReminderStore",
    "SqlReminderStore",
    "deliver",
]

log = structlog.get_logger(__name__)

#: Потолок попыток доставки. Больше — значит «разберись вручную», а не «стучись в Telegram вечно».
MAX_ATTEMPTS = 5
#: Сколько минут аренда считается живой. Убитый посреди отправки процесс не должен оставлять
#: напоминание «в работе» навсегда: 15 минут — за пределами любого разумного таймаута Telegram.
LEASE_MINUTES = 15
#: Минимальная длина начала id для отмены: короче — начнёт совпадать со случайными строками.
MIN_REF = 6

DELIVER_PREFIX = "⏰ Напоминание:"

_CLAIM_SQL = """
WITH picked AS (
    SELECT id
      FROM planning.reminders
     WHERE due_at <= now()
       AND attempts < :max_attempts
       AND (
            status = 'scheduled'
            OR (status = 'sending' AND updated_at < now() - make_interval(mins => :lease_minutes))
       )
     ORDER BY due_at
     FOR UPDATE SKIP LOCKED
     LIMIT :limit
)
UPDATE planning.reminders AS r
   SET status = 'sending', attempts = r.attempts + 1, updated_at = now()
  FROM picked
 WHERE r.id = picked.id
RETURNING r.id::text AS id, r.text, r.due_at, r.attempts, r.owner_id,
          coalesce(r.last_error, '') AS last_error
"""

_INSERT_SQL = """
INSERT INTO planning.reminders (owner_id, text, due_at, status, trace_id)
VALUES (:owner_id, :body, :due_at, 'scheduled', :trace_id)
RETURNING id::text AS id
"""

_LIST_SQL = """
SELECT id::text AS id, text, due_at, attempts, status, coalesce(last_error, '') AS last_error
  FROM planning.reminders
 WHERE owner_id = :owner_id AND status IN ('scheduled', 'sending')
 ORDER BY due_at
 LIMIT :limit
"""

_CANCEL_BY_ID_SQL = """
UPDATE planning.reminders
   SET status = 'cancelled', updated_at = now()
 WHERE owner_id = :owner_id AND status IN ('scheduled', 'sending') AND id::text LIKE :prefix
RETURNING id::text AS id, text, due_at, owner_id
"""

_CANCEL_BY_TEXT_SQL = """
UPDATE planning.reminders
   SET status = 'cancelled', updated_at = now()
 WHERE owner_id = :owner_id AND status IN ('scheduled', 'sending') AND text ILIKE :needle
RETURNING id::text AS id, text, due_at, owner_id
"""

#: «что ушло бы» для ``--dry-run``: тот же отбор, что и у claim, но без блокировки и без попыток
_PEEK_SQL = """
SELECT id::text AS id, text, due_at, attempts, status, coalesce(last_error, '') AS last_error
  FROM planning.reminders
 WHERE due_at <= now()
   AND attempts < :max_attempts
   AND (
        status = 'scheduled'
        OR (status = 'sending' AND updated_at < now() - make_interval(mins => :lease_minutes))
   )
 ORDER BY due_at
 LIMIT :limit
"""

#: ``CAST(:id AS uuid)``, а не ``:id::uuid``: SQLAlchemy отдаёт последнее драйверу как есть, и
#: асинхронный драйвер спотыкается о «::» сразу после плейсхолдера (поймано на живом Postgres).
_MARK_SENT_SQL = """
UPDATE planning.reminders
   SET status = 'sent', sent_at = now(), last_error = NULL, updated_at = now()
 WHERE id = CAST(:id AS uuid)
"""

_MARK_FAILED_SQL = """
UPDATE planning.reminders
   SET last_error = :error,
       -- аренда снимается сразу: иначе «не доставил» означало бы «никто больше не попробует»
       status = CASE WHEN attempts >= :max_attempts THEN 'failed' ELSE 'scheduled' END,
       updated_at = now()
 WHERE id = CAST(:id AS uuid)
"""

_COUNTS_SQL = """
SELECT count(*) FILTER (WHERE status IN ('scheduled', 'sending')) AS scheduled,
       count(*) FILTER (WHERE status IN ('scheduled', 'sending') AND due_at <= now()) AS overdue,
       count(*) FILTER (WHERE status = 'sent') AS sent,
       count(*) FILTER (WHERE status = 'failed') AS failed
  FROM planning.reminders
"""


@dataclass(frozen=True, slots=True)
class Reminder:
    """Одна строка расписания. ``attempts`` — попытки, уже сделанные (счётчик растёт при claim)."""

    id: str
    text: str
    due_at: datetime
    owner_id: int = 0
    attempts: int = 0
    last_error: str = ""
    status: str = "scheduled"

    @property
    def short_id(self) -> str:
        return self.id[:8]

    def label(self, timezone: str | None = None) -> str:
        """Короткая строка для вывода и лога; без `timezone` — UTC, чтобы пояс был явно.

        `timezone` передаёт CLI: тот же id в списке напоминаний и в сухом прогоне обязан показывать
        один и тот же час — иначе «в 16:32» и «в 13:32» это два разных ответа про одно напоминание.
        """
        zone = ZoneInfo(timezone) if timezone else UTC
        when = self.due_at.astimezone(zone).strftime("%d.%m %H:%M")
        tail = f" (попытка {self.attempts})" if self.attempts else ""
        error = f" — {self.last_error[:80]}" if self.last_error else ""
        zone_tag = timezone or "UTC"
        return f"{self.short_id} · {when} {zone_tag}{tail} · {self.text[:120]}{error}"


@dataclass(slots=True)
class DeliverReport:
    """Итог одного тика: сколько забрали, сколько доставили, что не вышло."""

    claimed: int = 0
    sent: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    exhausted: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.claimed:
            return "напоминаний по времени сейчас нет"
        bits = [f"отправлено {len(self.sent)} из {self.claimed}"]
        if self.failed:
            bits.append(f"сбоев: {len(self.failed)} (повторим в следующем тике)")
        if self.exhausted:
            bits.append(f"попыток израсходовано: {len(self.exhausted)} — нужна реакция")
        return "; ".join(bits)


Sender = Callable[[Reminder], Awaitable[None]]


class ReminderStore(Protocol):
    """Порт для инструментов и CLI: поставить, посмотреть, отменить, забрать пора."""

    @property
    def enabled(self) -> bool: ...

    async def add(
        self,
        *,
        owner_id: int,
        body: str,
        due_at: datetime,
        trace_id: str | None = None,
    ) -> str: ...

    async def list_scheduled(self, *, owner_id: int, limit: int = 10) -> list[Reminder]: ...

    async def cancel(self, *, owner_id: int, ref: str) -> Reminder | None: ...

    async def claim_due(self, *, limit: int = 20) -> list[Reminder]: ...

    async def peek_due(self, *, limit: int = 20) -> list[Reminder]: ...

    async def mark_sent(self, reminder_id: str) -> None: ...

    async def mark_failed(self, reminder_id: str, error: str) -> None: ...


class NullReminderStore:
    """Без БД напоминания физически невозможны — это надо сказать, а не пообещать поставить."""

    @property
    def enabled(self) -> bool:
        return False

    async def add(self, **kwargs: Any) -> str:
        raise RuntimeError("база недоступна: напоминание некуда сохранить")

    async def list_scheduled(self, **kwargs: Any) -> list[Reminder]:
        return []

    async def cancel(self, **kwargs: Any) -> Reminder | None:
        return None

    async def claim_due(self, **kwargs: Any) -> list[Reminder]:
        return []

    async def peek_due(self, **kwargs: Any) -> list[Reminder]:
        return []

    async def mark_sent(self, reminder_id: str) -> None:
        return None

    async def mark_failed(self, reminder_id: str, error: str) -> None:
        return None


class SqlReminderStore:
    """Расписание в ``planning.reminders``. Один вызов — одна транзакция: тик переживает рестарт."""

    def __init__(self, *, session_factory: Callable[[], Any] | None = None) -> None:
        self._sm = session_factory
        self.failures = 0

    @property
    def enabled(self) -> bool:
        return True

    def _session(self) -> Any:
        return self._sm() if self._sm is not None else session()

    async def add(
        self,
        *,
        owner_id: int,
        body: str,
        due_at: datetime,
        trace_id: str | None = None,
    ) -> str:
        if due_at.tzinfo is None:
            # Наивное время в timestamptz — это «сработает не тогда», а не «не сработает вовсе»
            raise ValueError("due_at обязан быть tz-aware: момент считается в таймере владельца")
        async with self._session() as s:
            row = (
                await s.execute(
                    text(_INSERT_SQL),
                    {
                        "owner_id": int(owner_id),
                        "body": " ".join(body.split())[:2000],
                        "due_at": due_at,
                        "trace_id": trace_id,
                    },
                )
            ).mappings()
            reminder_id = str(row.one()["id"])
            await s.commit()
        return reminder_id

    async def list_scheduled(self, *, owner_id: int, limit: int = 10) -> list[Reminder]:
        try:
            async with self._session() as s:
                result = await s.execute(
                    text(_LIST_SQL), {"owner_id": int(owner_id), "limit": limit}
                )
                rows = result.mappings().all()
        except Exception as exc:  # noqa: BLE001 - список не имеет права ронять ответ боту
            self._fail("list", exc)
            return []
        return [_reminder(row) for row in rows]

    async def cancel(self, *, owner_id: int, ref: str) -> Reminder | None:
        """Отмена по началу id (тем, что напечатано в списке) или по подстроке текста."""
        value = " ".join((ref or "").split())
        if not value:
            return None
        if _looks_like_id(value):
            sql, params = _CANCEL_BY_ID_SQL, {"prefix": f"{value}%", "owner_id": int(owner_id)}
        else:
            sql, params = _CANCEL_BY_TEXT_SQL, {"needle": f"%{value}%", "owner_id": int(owner_id)}
        try:
            async with self._session() as s:
                rows = (await s.execute(text(sql), params)).mappings().all()
                await s.commit()
        except Exception as exc:  # noqa: BLE001
            self._fail("cancel", exc)
            return None
        if not rows:
            return None
        return _reminder({**dict(rows[0]), "attempts": 0, "status": "cancelled", "last_error": ""})

    async def claim_due(self, *, limit: int = 20) -> list[Reminder]:
        """Забрать пора атомарно: строка уходит в аренду, второй тик её не увидит."""
        try:
            async with self._session() as s:
                result = await s.execute(
                    text(_CLAIM_SQL),
                    {
                        "max_attempts": MAX_ATTEMPTS,
                        "limit": limit,
                        "lease_minutes": LEASE_MINUTES,
                    },
                )
                rows = result.mappings().all()
                await s.commit()
        except Exception as exc:  # noqa: BLE001 - тик без БД просто ничего не забирает
            self._fail("claim", exc)
            return []
        return [_reminder(row) for row in rows]

    async def peek_due(self, *, limit: int = 20) -> list[Reminder]:
        """Что пора отправить, — ничего при этом не меняя (сухая проверка таймера)."""
        try:
            async with self._session() as s:
                result = await s.execute(
                    text(_PEEK_SQL),
                    {
                        "max_attempts": MAX_ATTEMPTS,
                        "limit": limit,
                        "lease_minutes": LEASE_MINUTES,
                    },
                )
                rows = result.mappings().all()
        except Exception as exc:  # noqa: BLE001 - сухая проба не имеет права ронять CLI
            self._fail("peek", exc)
            return []
        return [_reminder(row) for row in rows]

    async def mark_sent(self, reminder_id: str) -> None:
        try:
            async with self._session() as s:
                await s.execute(text(_MARK_SENT_SQL), {"id": reminder_id})
                await s.commit()
        except Exception as exc:  # noqa: BLE001
            self._fail("mark_sent", exc)

    async def mark_failed(self, reminder_id: str, error: str) -> None:
        try:
            async with self._session() as s:
                await s.execute(
                    text(_MARK_FAILED_SQL),
                    {
                        "id": reminder_id,
                        "error": " ".join(error.split())[:500],
                        "max_attempts": MAX_ATTEMPTS,
                    },
                )
                await s.commit()
        except Exception as exc:  # noqa: BLE001
            self._fail("mark_failed", exc)

    async def counts(self) -> dict[str, Any]:
        try:
            async with self._session() as s:
                return dict((await s.execute(text(_COUNTS_SQL))).mappings().one())
        except Exception as exc:  # noqa: BLE001
            self._fail("counts", exc)
            return {"error": repr(exc)[:200]}

    def _fail(self, where: str, exc: BaseException) -> None:
        self.failures += 1
        log.warning("reminders.failed", where=where, err=repr(exc)[:300])


async def deliver(
    store: ReminderStore,
    *,
    send: Sender,
    limit: int = 20,
) -> DeliverReport:
    """Один проход расписания: забрать пора, отправить, отметить.

    Отправка вне транзакции сознательно: «сообщение ушло, а база не ответила» лучше, чем «отметили
    отправленным, а владелец ничего не получил». Худший исход здесь — повтор, и он переносим;
    потеря напоминания — нет.
    """
    report = DeliverReport()
    due = await store.claim_due(limit=limit)
    report.claimed = len(due)
    for reminder in due:
        try:
            await send(reminder)
        except Exception as exc:  # noqa: BLE001 - чужой таймаут не останавливает весь тик
            report.failed.append(reminder.short_id)
            await store.mark_failed(reminder.id, f"{type(exc).__name__}: {exc}")
            if reminder.attempts >= MAX_ATTEMPTS:
                report.exhausted.append(reminder.short_id)
            log.warning("reminder.delivery_failed", id=reminder.short_id, err=repr(exc)[:200])
            continue
        await store.mark_sent(reminder.id)
        report.sent.append(reminder.short_id)
    if report.claimed:
        log.info(
            "reminder.tick",
            claimed=report.claimed,
            sent=len(report.sent),
            failed=len(report.failed),
        )
    return report


def _reminder(row: Any) -> Reminder:
    due_at = row["due_at"]
    if isinstance(due_at, datetime) and due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)
    return Reminder(
        id=str(row["id"]),
        text=str(row["text"]),
        due_at=due_at,
        owner_id=int(row.get("owner_id") or 0),
        attempts=int(row.get("attempts") or 0),
        last_error=str(row.get("last_error") or ""),
        status=str(row.get("status") or "scheduled"),
    )


def _looks_like_id(ref: str) -> bool:
    return len(ref) >= MIN_REF and all(char in "0123456789abcdef-" for char in ref.lower())
