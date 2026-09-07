"""Крон-прогоны агента (``planning.job``): «каждое утро в 8 — сводка» без внешнего cron.

Философия та же, что у наблюдателей: очередь в базе, аренда через FOR UPDATE SKIP LOCKED,
созревший ряд переводится вперёд ОДИН В ОДИН коммит с выборкой — два процесса не выполнят
одно и то же, а рестарт посреди тика не устроит «выполнилось дважды». Цена честности:
между bump'ом и исполнением процесс мог умереть — этот пропуск не повторится (at-most-once);
для «ровно один раз» нужен внешний оркестратор, и мы это не изображаем.

Расписание — не cron-магия, а шесть форм, которые владелец произносит словами:
once / every_min / hourly / daily HH:MM / weekdays HH:MM / weekly DOW HH:MM (1=понедельник).
Время локальное cfg.tz: «утро в 8» — это 8 утра у владельца, а не у UTC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from aegis.platform.db import session

__all__ = ["JobRow", "SqlJobStore", "schedule_next"]

REPEATS = ("once", "every_min", "hourly", "daily", "weekdays", "weekly")

_AT_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

_AUTO_PAUSE_AFTER = 5  # noqa: PLR2004 — пять подряд упавших прогонов: стоп, а не стучать вечно


def _at_minutes(at_time: str) -> tuple[int, int]:
    m = _AT_RE.match(at_time.strip())
    if not m:
        raise ValueError(f"нужно «ЧЧ:ММ», а не «{at_time}»")
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:  # noqa: PLR2004 — границы часов/минут
        raise ValueError(f"вне диапазона часов/минут: «{at_time}»")
    return hh, mm


def schedule_next(
    *,
    repeat: str,
    at_time: str = "",
    every_min: int = 30,
    dow: int = 1,
    after: datetime,
    tz: Any,
) -> datetime | None:
    """Следующий запуск строго после ``after`` (в локальной зоне). once — None (ряд гаснет)."""
    if repeat not in REPEATS:
        raise ValueError(f"repeat обязан быть одним из {REPEATS}")
    local = after.astimezone(tz)
    if repeat == "once":
        return None
    if repeat == "every_min":
        step = max(1, int(every_min))
        base = local.replace(second=0, microsecond=0)
        return (base + timedelta(minutes=step)).astimezone(UTC)
    if repeat == "hourly":
        base = local.replace(minute=0, second=0, microsecond=0)
        return (base + timedelta(hours=1)).astimezone(UTC)
    hh, mm = _at_minutes(at_time) if at_time else (8, 0)  # noqa: PLR2004 — «08:00» добрый default
    today = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if repeat == "weekly":
        delta = (int(dow) - 1 - today.weekday()) % 7
        cand = today + timedelta(days=delta)
        if cand <= local:
            cand += timedelta(days=7)
        return cand.astimezone(UTC)
    if today <= local:
        today += timedelta(days=1)
    if repeat == "weekdays":
        while today.weekday() >= 5:  # noqa: PLR2004 — суббота/воскресенье
            today += timedelta(days=1)
    return today.astimezone(UTC)


@dataclass(slots=True)
class JobRow:
    id: str
    owner_id: int
    title: str
    prompt: str
    repeat: str
    at_time: str = ""
    every_min: int = 30
    dow: int = 1
    channel: str = "message"
    status: str = "active"
    next_run: datetime | None = None
    last_run: datetime | None = None
    last_error: str = ""
    fail_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "title": self.title,
            "prompt": self.prompt,
            "repeat": self.repeat,
            "at_time": self.at_time,
            "every_min": self.every_min,
            "dow": self.dow,
            "channel": self.channel,
        }


def _row(r: Any) -> JobRow:
    return JobRow(
        id=str(r["id"]),
        owner_id=int(r["owner_id"]),
        title=str(r["title"]),
        prompt=str(r["prompt"]),
        repeat=str(r["repeat"]),
        at_time=str(r["at_time"] or ""),
        every_min=int(r["every_min"]),
        dow=int(r["dow"]),
        channel=str(r["channel"]),
        status=str(r["status"]),
        next_run=r["next_run"],
        last_run=r["last_run"],
        last_error=str(r["last_error"] or ""),
        fail_count=int(r["fail_count"]),
    )


class SqlJobStore:
    """Одна строка — один ряд ``planning.job``. Методы маленькие; вся хитрость в claim_due."""

    def __init__(self, *, session_factory: Any = None) -> None:
        self._sm = session_factory

    def _session(self) -> Any:
        return self._sm() if self._sm is not None else session()

    async def add(
        self,
        *,
        owner_id: int,
        title: str,
        prompt: str,
        repeat: str = "daily",
        at_time: str = "08:00",
        every_min: int = 30,
        dow: int = 1,
        channel: str = "message",
        first_run: datetime | None = None,
        tz: Any = UTC,
    ) -> str:
        if repeat not in REPEATS:
            raise ValueError(f"repeat обязан быть одним из {REPEATS}")
        if first_run is None:
            first_run = schedule_next(
                repeat=repeat,
                at_time=at_time,
                every_min=every_min,
                dow=dow,
                after=datetime.now(UTC),
                tz=tz,
            )
        if first_run is None:
            raise ValueError("once требует явный first_run")
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "INSERT INTO planning.job (owner_id, title, prompt, repeat, at_time,"
                            " every_min, dow, channel, next_run) VALUES (:o, :t, :p, :r, :at, :em,"
                            " :dow, :c, :nr) ON CONFLICT (owner_id, title) DO UPDATE SET"
                            " prompt = :p, repeat = :r, at_time = :at, every_min = :em, dow = :dow,"
                            " channel = :c, status = 'active', next_run = :nr, last_error = '',"
                            " fail_count = 0, updated_at = now() RETURNING id::text AS id"
                        ),
                        {
                            "o": int(owner_id),
                            "t": title.strip(),
                            "p": prompt.strip(),
                            "r": repeat,
                            "at": at_time.strip(),
                            "em": int(every_min),
                            "dow": int(dow),
                            "c": channel,
                            "nr": first_run,
                        },
                    )
                )
                .mappings()
                .one()
            )
            await s.commit()
        return str(row["id"])

    async def list_jobs(self, *, owner_id: int, limit: int = 20) -> list[JobRow]:
        async with self._session() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            "SELECT id, owner_id, title, prompt, repeat, at_time, every_min, dow,"
                            " channel, status, next_run, last_run, last_error, fail_count"
                            " FROM planning.job WHERE owner_id = :o"
                            " ORDER BY (status = 'active') DESC, next_run NULLS LAST LIMIT :n"
                        ),
                        {"o": int(owner_id), "n": int(limit)},
                    )
                )
                .mappings()
                .all()
            )
        return [_row(r) for r in rows]

    async def set_status(self, *, owner_id: int, ref: str, status: str) -> str | None:
        if status not in ("active", "paused", "done"):
            raise ValueError("status: active|paused|done")
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "UPDATE planning.job SET status = :st, updated_at = now()"
                            " WHERE id = (SELECT id FROM planning.job WHERE owner_id = :o"
                            " AND (id::text LIKE :ref || '%' OR title ILIKE '%' || :ref"
                            " || '%') AND status <> :st ORDER BY (id::text LIKE :ref ||"
                            " '%') DESC LIMIT 1) RETURNING title"
                        ),
                        {"st": status, "o": int(owner_id), "ref": ref.strip()},
                    )
                )
                .mappings()
                .first()
            )
            await s.commit()
        if row is None:
            return None
        verb = {"active": "возобновлено", "paused": "на паузе", "done": "закрыто"}[status]
        return f"«{row['title']}»: {verb}"

    async def resolve(self, *, owner_id: int, ref: str) -> JobRow | None:
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "SELECT id, owner_id, title, prompt, repeat, at_time, every_min, dow,"
                            " channel, status, next_run, last_run, last_error, fail_count"
                            " FROM planning.job WHERE owner_id = :o"
                            " AND (id::text LIKE :ref || '%' OR title ILIKE '%' || :ref || '%')"
                            " ORDER BY (id::text LIKE :ref || '%') DESC LIMIT 1"
                        ),
                        {"o": int(owner_id), "ref": ref.strip()},
                    )
                )
                .mappings()
                .first()
            )
        return _row(row) if row is not None else None

    async def trigger_now(self, *, owner_id: int, ref: str) -> str | None:
        """Поставить в очередь на ближайший тик: next_run = сейчас. Честно: не «выполнено»."""
        now = datetime.now(UTC)
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "UPDATE planning.job SET next_run = :now, status = 'active',"
                            " updated_at = now() WHERE id = (SELECT id FROM planning.job"
                            " WHERE owner_id = :o AND (id::text LIKE :ref || '%' OR title"
                            " ILIKE '%' || :ref || '%') ORDER BY (id::text LIKE :ref ||"
                            " '%') DESC LIMIT 1) RETURNING title"
                        ),
                        {"now": now, "o": int(owner_id), "ref": ref.strip()},
                    )
                )
                .mappings()
                .first()
            )
            await s.commit()
        return f"«{row['title']}» в очереди — ближайший тик" if row else None

    async def drop(self, *, owner_id: int, ref: str) -> bool:
        async with self._session() as s:
            res = await s.execute(
                text(
                    "DELETE FROM planning.job WHERE id = (SELECT id FROM planning.job"
                    " WHERE owner_id = :o AND (id::text LIKE :ref || '%' OR title ILIKE"
                    " '%' || :ref || '%') ORDER BY (id::text LIKE :ref || '%') DESC LIMIT 1)"
                ),
                {"o": int(owner_id), "ref": ref.strip()},
            )
            await s.commit()
        return bool((res.rowcount or 0) > 0)

    async def claim_due(self, *, limit: int, now: datetime, tz: Any) -> list[JobRow]:
        """Созревшие ряды; next_run переводится вперёд этим же коммитом (защита от двойного
        исполнения и от «зацикливания» упавшего прогона). once после выдачи гаснет."""
        out: list[JobRow] = []
        async with self._session() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            "SELECT id, owner_id, title, prompt, repeat, at_time, every_min, dow,"
                            " channel, status, next_run, last_run, last_error, fail_count"
                            " FROM planning.job WHERE status = 'active' AND next_run <= :now"
                            " ORDER BY next_run LIMIT :n FOR UPDATE SKIP LOCKED"
                        ),
                        {"now": now, "n": int(limit)},
                    )
                )
                .mappings()
                .all()
            )
            for r in rows:
                job = _row(r)
                nxt = schedule_next(
                    repeat=job.repeat,
                    at_time=job.at_time,
                    every_min=job.every_min,
                    dow=job.dow,
                    after=now,
                    tz=tz,
                )
                if nxt is None:  # once: ряд отработает и закроется
                    await s.execute(
                        text(
                            "UPDATE planning.job SET status = 'done', last_run = :now,"
                            " updated_at = now() WHERE id = :i"
                        ),
                        {"now": now, "i": job.id},
                    )
                else:
                    await s.execute(
                        text(
                            "UPDATE planning.job SET next_run = :nr, last_run = :now,"
                            " updated_at = now() WHERE id = :i"
                        ),
                        {"nr": nxt, "now": now, "i": job.id},
                    )
                out.append(job)
            await s.commit()
        return out

    async def finish_run(self, job: JobRow, *, ok: bool, error: str) -> None:
        async with self._session() as s:
            if ok:
                await s.execute(
                    text("UPDATE planning.job SET fail_count = 0, last_error = '' WHERE id = :i"),
                    {"i": job.id},
                )
            else:
                await s.execute(
                    text(
                        "UPDATE planning.job SET fail_count = fail_count + 1, last_error = :e,"
                        " status = CASE WHEN fail_count + 1 >= :cap THEN 'paused' ELSE status END"
                        " WHERE id = :i"
                    ),
                    {"e": error[:500], "cap": _AUTO_PAUSE_AFTER, "i": job.id},
                )
            await s.commit()
