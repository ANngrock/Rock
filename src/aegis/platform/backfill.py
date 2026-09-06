"""Бэкфиллы (F10): фоновые батчи на механике аренд — той же, что у напоминаний.

Expand-миграция не имеет права превращаться в «UPDATE на миллион строк внутри alembic»: это
lock'и на живой базе и деплой, который невозможно прервать. Поэтому «долгая работа поверх
миграции» вынесена в отдельный контракт:

* реестр заданий в коде (:data:`BACKFILL_SPECS`) + строка состояния в ``platform.backfills``;
* аренда (``FOR UPDATE SKIP LOCKED``) и курсор: два процесса не грызут один батч, а рестарт не
  начинает заново;
* ``--dry-run`` показывает план (сколько строк под условием, сколько возьмёт батч) и не трогает
  ничего; ``pause``/`resume` — чтобы можно было отложить на окно тишины;
* прогресс — строки и раунды, и он же — проба ``aegis migrate status``: «сколько строк в
  незавершённом бэкфилле» обязано читаться с экрана, а не из догадок.

SQL батча — шаблон с обязательными плейсхолдерами ``:cursor`` и ``:limit``: курсор хранится как
jsonb, и «кто следующий» решает сама база, а не память процесса.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import orjson
import structlog
from sqlalchemy import text

from aegis.platform.db import SessionFactory, session

__all__ = ["BackfillJob", "BackfillSpec", "BACKFILL_SPECS", "NullBackfillStore", "SqlBackfillStore"]

log = structlog.get_logger(__name__)

_CLAIM_SQL = """
WITH picked AS (
    SELECT name
      FROM platform.backfills
     WHERE status = 'pending'
       AND (CAST(:name AS text) IS NULL OR name = CAST(:name AS text))
       AND (lease_until IS NULL OR lease_until < now())
     ORDER BY name
     FOR UPDATE SKIP LOCKED
     LIMIT 1
)
UPDATE platform.backfills AS b
   SET status = 'running', lease_owner = :owner, lease_until = now() + make_interval(secs => :ttl)
  FROM picked
 WHERE b.name = picked.name
RETURNING b.name, b.target, b.batch_size, b.cursor, b.rows_done
"""

_RELEASE_DONE = """
UPDATE platform.backfills
   SET status = 'done', rows_done = :rows_done, rounds = rounds + 1, cursor = CAST(:cursor AS
   jsonb),
       lease_until = NULL, last_error = NULL, updated_at = now()
 WHERE name = :name AND lease_owner = :owner
"""

_RELEASE_NEXT = """
UPDATE platform.backfills
   SET status = 'pending', rows_done = :rows_done, rounds = rounds + 1,
       cursor = CAST(:cursor AS jsonb), lease_until = NULL, updated_at = now()
 WHERE name = :name AND lease_owner = :owner
"""

_PAUSE = """
UPDATE platform.backfills SET status = 'paused', updated_at = now()
 WHERE name = :name AND lease_owner = :owner
"""

_FAILED = """
UPDATE platform.backfills
   SET status = 'failed', last_error = :err, lease_until = NULL, updated_at = now()
 WHERE name = :name AND lease_owner = :owner
"""

_REAP = """
UPDATE platform.backfills
   SET status = 'pending', lease_until = NULL, updated_at = now(),
       last_error = 'lease expired (владелец умер?)'
 WHERE status = 'running' AND lease_until < now()
"""

_STATUS_SQL = """
SELECT name, status, target, batch_size, rows_done, rounds, cursor, last_error,
       lease_owner, lease_until, updated_at
  FROM platform.backfills ORDER BY name
"""


#: «осталось строк» — COUNT(*) по описанию цели невозможен универсально; вместо этого — функция
#: из спека, которая умеет честно сказать None («не считаем»): врать числом хуже, чем не иметь числа
@dataclass(frozen=True, slots=True)
class BackfillSpec:
    name: str
    #: человекочитаемое «что бэкфиллим» — в ``migrate status`` и в логах
    target: str
    #: SQL батча. Обязательные параметры: :cursor (jsonb), :limit. Возвращает RETURNING id —
    #  по нему двигается курсор; пустой результат = «дело сделано»
    sql: str
    batch_size: int = 500
    #: SQL подсчёта остатка для отчётов (опционально; обязан быть SELECT count)
    remaining_sql: str | None = None
    #: идемпотентность повторного применения батча: если False, курсор обязан продвигаться строго
    idempotent: bool = True


BACKFILL_SPECS: dict[str, BackfillSpec] = {
    spec.name: spec
    for spec in (
        BackfillSpec(
            name="events_owner",
            target="platform.events.owner_id из stream_id 'owner:<id>'",
            sql="""
                WITH batch AS (
                    SELECT e.id
                      FROM platform.events e
                     WHERE e.id > CAST(:cursor AS text)::bigint
                       AND e.owner_id = 0
                       AND e.stream_id LIKE 'owner:%'
                     ORDER BY e.id
                     LIMIT :limit
                )
                UPDATE platform.events AS e
                   SET owner_id = split_part(e.stream_id, ':', 2)::bigint
                  FROM batch
                 WHERE e.id = batch.id
                RETURNING e.id
            """,
            remaining_sql="""
                SELECT count(*) FROM platform.events
                 WHERE owner_id = 0 AND stream_id LIKE 'owner:%'
            """,
        ),
        BackfillSpec(
            name="journal_actor",
            target=(
                "governance.decision_records.actor_id: эпоха «владелец и всё» — спрашивал владелец"
            ),
            sql="""
                WITH batch AS (
                    SELECT dr.id::text AS id
                      FROM governance.decision_records dr
                     WHERE dr.actor_id IS NULL
                       AND dr.seq > CAST(:cursor AS text)::bigint
                     ORDER BY dr.seq
                     LIMIT :limit
                )
                UPDATE governance.decision_records AS dr
                   SET actor_id = dr.owner_id
                  FROM batch
                 WHERE dr.id::text = batch.id
                RETURNING dr.seq::text
            """,
            remaining_sql="""
                SELECT count(*) FROM governance.decision_records WHERE actor_id IS NULL
            """,
        ),
    )
}

_EPOCH_CURSOR = {"v": "0"}


@dataclass(frozen=True, slots=True)
class BackfillJob:
    name: str
    status: str
    target: str
    batch_size: int
    rows_done: int
    rounds: int
    cursor: Mapping[str, Any]
    last_error: str | None = None
    lease_owner: str | None = None
    lease_until: Any = None

    def as_row(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "target": self.target,
            "batch_size": self.batch_size,
            "rows_done": self.rows_done,
            "rounds": self.rounds,
            "cursor": dict(self.cursor),
            "last_error": self.last_error,
            "lease_owner": self.lease_owner,
            "lease_until": str(self.lease_until or ""),
        }


class BackfillStore(Protocol):
    async def register(self, spec: BackfillSpec) -> None: ...

    async def count_remaining(self, spec: BackfillSpec) -> int | None: ...

    async def claim(
        self, *, owner: str, lease_secs: int = 900, name: str | None = None
    ) -> BackfillJob | None: ...

    async def finish_batch(
        self, job: BackfillJob, rows: int, cursor: Mapping[str, Any], *, more: bool
    ) -> None: ...

    async def fail(self, job: BackfillJob, error: str) -> None: ...

    async def pause(self, job: BackfillJob) -> None: ...

    async def reap_expired(self) -> int: ...

    async def status(self) -> list[BackfillJob]: ...


class NullBackfillStore:
    """Без БД бэкфиллам негде жить. «Тишина» была бы враньём — status говорит ровно это."""

    durable = False

    async def register(self, spec: BackfillSpec) -> None:
        return None

    async def count_remaining(self, spec: BackfillSpec) -> int | None:
        del spec
        return None

    async def claim(
        self, *, owner: str, lease_secs: int = 900, name: str | None = None
    ) -> BackfillJob | None:
        del owner, lease_secs, name
        return None

    async def finish_batch(
        self, job: BackfillJob, rows: int, cursor: Mapping[str, Any], *, more: bool
    ) -> None:
        return None

    async def fail(self, job: BackfillJob, error: str) -> None:
        return None

    async def pause(self, job: BackfillJob) -> None:
        return None

    async def reap_expired(self) -> int:
        return 0

    async def status(self) -> list[BackfillJob]:
        return []


class SqlBackfillStore:
    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._sm = session_factory

    def _session(self) -> Any:
        return self._sm() if self._sm is not None else session()

    async def register(self, spec: BackfillSpec) -> None:
        """INSERT, если строки нет: повторная регистрация не должна обнулять прогресс.

        Смена batch_size на существующей задаче — тоже осознанный NO: «ускорить бэкфилл»
        = правка спека + ``aegis backfill reset``, чтобы сброс был виден.
        """
        async with self._session() as s:
            await s.execute(
                text(
                    "INSERT INTO platform.backfills (name, target, batch_size, cursor, status)"
                    " VALUES (:name, :target, :batch, CAST(:cursor AS jsonb), 'pending')"
                    " ON CONFLICT (name) DO NOTHING"
                ).bindparams(
                    name=spec.name,
                    target=spec.target[:500],
                    batch=int(spec.batch_size),
                    cursor=orjson.dumps(dict(_EPOCH_CURSOR)).decode(),
                )
            )
            await s.commit()

    async def claim(
        self, *, owner: str, lease_secs: int = 900, name: str | None = None
    ) -> BackfillJob | None:
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(_CLAIM_SQL).bindparams(
                            owner=owner[:120], ttl=int(lease_secs), name=name
                        )
                    )
                )
                .mappings()
                .first()
            )
            await s.commit()
        if row is None:
            return None
        cursor_raw = row["cursor"] if isinstance(row["cursor"], Mapping) else _EPOCH_CURSOR
        return BackfillJob(
            name=str(row["name"]),
            status="running",
            target=str(row["target"]),
            batch_size=int(row["batch_size"]),
            rows_done=int(row["rows_done"] or 0),
            rounds=0,
            cursor=dict(cursor_raw or {}),
        )

    async def finish_batch(
        self, job: BackfillJob, rows: int, cursor: Mapping[str, Any], *, more: bool
    ) -> None:
        sql = _RELEASE_NEXT if more else _RELEASE_DONE
        async with self._session() as s:
            await s.execute(
                text(sql).bindparams(
                    name=job.name,
                    owner=_owner_tag(),
                    rows_done=int(job.rows_done) + int(rows),
                    cursor=orjson.dumps(dict(cursor)).decode(),
                )
            )
            await s.commit()

    async def fail(self, job: BackfillJob, error: str) -> None:
        async with self._session() as s:
            await s.execute(
                text(_FAILED).bindparams(name=job.name, owner=_owner_tag(), err=error[:2000])
            )
            await s.commit()

    async def pause(self, job: BackfillJob) -> None:
        async with self._session() as s:
            await s.execute(text(_PAUSE).bindparams(name=job.name, owner=_owner_tag()))
            await s.commit()

    async def count_remaining(self, spec: BackfillSpec) -> int | None:
        if not spec.remaining_sql:
            return None
        try:
            async with self._session() as s:
                return int(await s.scalar(text(spec.remaining_sql)) or 0)
        except Exception as exc:  # noqa: BLE001 — остаток не обязан быть посчитанным ценой status
            log.warning("backfill.remaining_failed", name=spec.name, err=repr(exc)[:160])
            return None

    async def reap_expired(self, spec: BackfillSpec | None = None) -> int:
        del spec
        async with self._session() as s:
            result = await s.execute(text(_REAP))
            await s.commit()
            return int(result.rowcount or 0)

    async def status(self) -> list[BackfillJob]:
        try:
            async with self._session() as s:
                rows = (await s.execute(text(_STATUS_SQL))).mappings().all()
        except Exception as exc:  # noqa: BLE001 — «нет таблицы» — не повод падать в /status
            log.warning("backfill.status_failed", err=repr(exc)[:160])
            return []
        out: list[BackfillJob] = []
        for row in rows:
            cursor_raw = row["cursor"] if isinstance(row["cursor"], Mapping) else {}
            out.append(
                BackfillJob(
                    name=str(row["name"]),
                    status=str(row["status"]),
                    target=str(row["target"]),
                    batch_size=int(row["batch_size"]),
                    rows_done=int(row["rows_done"] or 0),
                    rounds=int(row["rounds"] or 0),
                    cursor=dict(cursor_raw),
                    last_error=str(row["last_error"] or "") or None,
                    lease_owner=str(row["lease_owner"] or "") or None,
                    lease_until=row["lease_until"],
                )
            )
        return out


async def run_backfill(
    store: BackfillStore,
    *,
    names: Sequence[str] | None = None,
    batches: int = 1,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Прогнать по ``batches`` батчей на задание. Отчёт — список dict'ов (CLI и doctor читают его).

    Сухой прогон не берёт аренду: «посмотреть план» не должно мешать рабочему процессу и не должно
    выглядеть как «процесс завис на батче».
    """
    targets = [BACKFILL_SPECS[n] for n in (names or list(BACKFILL_SPECS))]
    report: list[dict[str, Any]] = []
    for spec in targets:
        if dry_run:
            report.append(
                {
                    "name": spec.name,
                    "dry_run": True,
                    "batch_size": spec.batch_size,
                    "remaining": await store.count_remaining(spec),
                }
            )
            continue
        await store.register(spec)
        done = 0
        last_error = ""
        for _ in range(max(1, int(batches))):
            job = await store.claim(owner=_owner_tag(), name=spec.name)
            if job is None:
                break
            try:
                rows = await _apply_batch(spec, job)
            except Exception as exc:  # noqa: BLE001 — одна ошибка не превращается в «всё зависло»
                await store.fail(job, f"{type(exc).__name__}: {exc}")
                last_error = repr(exc)[:200]
                break
            moved = len(rows)
            done += moved
            new_cursor = {"v": str(cursor_value(rows) or _EPOCH_CURSOR["v"])}
            await store.finish_batch(job, moved, new_cursor, more=moved >= spec.batch_size)
            if moved < spec.batch_size:
                break
        entry: dict[str, Any] = {
            "name": spec.name,
            "batches_done": done,
            "remaining": await store.count_remaining(spec),
        }
        if last_error:
            entry["error"] = last_error
        report.append(entry)
    return report


async def _apply_batch(spec: BackfillSpec, job: BackfillJob) -> list[Any]:
    sm = session
    cursor = str(job.cursor.get("v") or _EPOCH_CURSOR["v"])
    async with sm() as s:
        result = await s.execute(
            text(spec.sql).bindparams(cursor=cursor, limit=int(job.batch_size or spec.batch_size))
        )
        rows = [(row[0] if len(row) == 1 else tuple(row)) for row in result.fetchall()]
        await s.commit()
    return rows


def _owner_tag() -> str:
    import os  # noqa: PLC0415
    import socket  # noqa: PLC0415

    return f"{socket.gethostname()}:{os.getpid()}:{int(time.time()) // 60}"


def cursor_value(rows: Sequence[Any]) -> Any:
    """Последний RETURNING-элемент батча = новый курсор. Пусто — курсор не двигаем."""
    if not rows:
        return None
    last = rows[-1]
    if isinstance(last, (list, tuple)):
        last = last[0]
    return str(last)
