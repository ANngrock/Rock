"""Реестр исполненных активностей (F8): «не дважды, а ровно один раз — уже исполнено».

Таблица ``platform.activity_ledger`` — то же обещание, что у idempotency-key в платёжных системах:
первые пришедший процесс исполняет activity и записывает результат; каждый следующий получает
сохранённый результат и НЕ исполняет ничего. Крах между «действие сделано» и «результат записан»
закрывается состоянием ``running`` с lease: живой владелец продолжит, мёртвый — отдаст строку
по истечению аренды (reap), и повтор сверится по fencing-токену.

Здесь нет знаний о том, ЧТО за действие: ledger — общий механизм для tools (платёж!), отправки
ответа и последующих компенсаций. «Платёж не уйдёт дважды» требует ровно этого контракта и
ничего больше.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import orjson
from sqlalchemy import text

from aegis.platform.db import SessionFactory, session

__all__ = ["ActivityOutcome", "ActivityState", "Ledger", "NullLedger", "SqlLedger", "run_once"]


@dataclass(frozen=True, slots=True)
class ActivityState:
    activity_id: str
    state: str  # 'running' | 'done' | 'compensated'
    fencing_token: int
    result: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ActivityOutcome:
    state: str
    result: dict[str, Any]
    replayed: bool  # True — вернули сохранённый результат, эффект НЕ исполнялся заново

    @property
    def done(self) -> bool:
        return self.state == "done"


class Ledger(Protocol):
    async def start(
        self, activity_id: str, *, fencing_token: int = 0, trace_id: str = ""
    ) -> ActivityState | None:
        """Занять activity под исполнение. ``None`` — можно исполнять; объект — уже занято/сделано.

        Гонка «два процесса одновременно» решается ON CONFLICT: победитель вставки исполняет,
        проигравший читает состояние и либо получает готовый результат (done), либо видит чужой
        running (пропускаем — дедупликация важнее скорости).
        """
        sql = """
        INSERT INTO platform.activity_ledger (activity_id, trace_id, state, fencing_token, owner)
        VALUES (:id, CAST(:trace AS uuid), 'running', :fence, :owner)
        ON CONFLICT (activity_id) DO NOTHING
        RETURNING activity_id
        """
        async with self._session() as s:
            taken = await s.scalar(
                text(sql).bindparams(
                    id=activity_id[:240],
                    trace=trace_id if _is_uuid(trace_id) else None,
                    fence=int(fencing_token),
                    owner=self._owner(),
                )
            )
            await s.commit()
            if taken is not None:
                return None
            row = (
                (
                    await s.execute(
                        text(
                            "SELECT state, fencing_token, result FROM platform.activity_ledger"
                            " WHERE activity_id = :id"
                        ).bindparams(id=activity_id[:240])
                    )
                )
                .mappings()
                .first()
            )
            if row is None:  # гонка «до меня успели и отпустили»
                return None
            state = str(row["state"])
            result = row["result"] if isinstance(row["result"], dict) else None
            if state == "failed":
                # «падал» ≠ «чужой навсегда»: забираем терминальную строку себе новым токеном.
                # Условие по state в UPDATE — та же CAS, что у reminders: два реактиватора не
                # разминутся
                grabbed = await s.scalar(
                    text(
                        "UPDATE platform.activity_ledger SET state = 'running',"
                        " fencing_token = :fence, owner = :owner, attempts = attempts + 1"
                        " WHERE activity_id = :id AND state = 'failed' RETURNING activity_id"
                    ).bindparams(
                        id=activity_id[:240], fence=int(fencing_token), owner=self._owner()
                    )
                )
                await s.commit()
                if grabbed is not None:
                    return None
            return ActivityState(
                activity_id=activity_id,
                state=state,
                fencing_token=int(row["fencing_token"] or 0),
                result=dict(result or {}),
            )

    async def complete(
        self, activity_id: str, result: dict[str, Any], *, fencing_token: int = 0
    ) -> None: ...

    async def fail(self, activity_id: str, error: str, *, fencing_token: int = 0) -> None: ...

    async def compensate(self, activity_id: str, note: str = "") -> None: ...

    async def reap_expired(self, lease_s: int = 900) -> int: ...

    #: идемпотентность переживает рестарт только здесь; False — память процесса
    durable: bool


class NullLedger:
    """Память процесса: demo без БД. Идемпотентность «до рестарта» — и только; durable=False."""

    durable = False

    def __init__(self) -> None:
        self._rows: dict[str, ActivityState] = {}

    async def start(
        self, activity_id: str, *, fencing_token: int = 0, trace_id: str = ""
    ) -> ActivityState | None:
        del trace_id
        existing = self._rows.get(activity_id)
        if existing is not None:
            return existing  # «уже исполнено» или «в работе у этого же процесса» — см. turn.py
        self._rows[activity_id] = ActivityState(activity_id, "running", fencing_token)
        return None  # None = занят нами: «исполняй»

    async def complete(
        self, activity_id: str, result: dict[str, Any], *, fencing_token: int = 0
    ) -> None:
        self._rows[activity_id] = ActivityState(activity_id, "done", fencing_token, dict(result))

    async def fail(self, activity_id: str, error: str, *, fencing_token: int = 0) -> None:
        del error
        self._rows.pop(activity_id, None)

    async def compensate(self, activity_id: str, note: str = "") -> None:
        del note
        current = self._rows.get(activity_id)
        if current is not None:
            self._rows[activity_id] = ActivityState(
                activity_id, "compensated", current.fencing_token, current.result
            )

    async def reap_expired(self, lease_s: int = 900) -> int:
        del lease_s
        return 0


class SqlLedger:
    """Postgres-реестр. Одна операция = одна транзакция; «взял и выполнил» неразрывны по design."""

    durable = True

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._sm = session_factory

    def _session(self) -> Any:
        return self._sm() if self._sm is not None else session()

    @staticmethod
    def _owner() -> str:
        return f"{socket.gethostname()}:{os.getpid()}"

    async def start(
        self, activity_id: str, *, fencing_token: int = 0, trace_id: str = ""
    ) -> ActivityState | None:
        """Занять activity под исполнение. ``None`` — можно исполнять; объект — уже занято/сделано.

        Гонка «два процесса одновременно» решается ON CONFLICT: победитель вставки исполняет,
        проигравший читает состояние и либо ждёт (running + живая аренда → «не твоя очередь,
        пропусти»), либо получает готовый результат.
        """
        sql = """
        INSERT INTO platform.activity_ledger (activity_id, trace_id, state, fencing_token, owner)
        VALUES (:id, CAST(:trace AS uuid), 'running', :fence, :owner)
        ON CONFLICT (activity_id) DO NOTHING
        RETURNING activity_id
        """
        async with self._session() as s:
            taken = await s.scalar(
                text(sql).bindparams(
                    id=activity_id[:240],
                    trace=trace_id if _is_uuid(trace_id) else None,
                    fence=int(fencing_token),
                    owner=self._owner(),
                )
            )
            await s.commit()
            if taken is not None:
                return None
            row = (
                (
                    await s.execute(
                        text(
                            "SELECT state, fencing_token, result FROM platform.activity_ledger"
                            " WHERE activity_id = :id"
                        ).bindparams(id=activity_id[:240])
                    )
                )
                .mappings()
                .first()
            )
        if (
            row is None
        ):  # гонка «до меня успели и отпустили»: пробуем занять ещё раз на следующем проходе
            return None
        state = str(row["state"])
        result = row["result"] if isinstance(row["result"], dict) else None
        if state == "failed":
            # «падал» ≠ «чужой навсегда»: забираем терминальную строку себе новым токеном.
            # Условие по state в UPDATE — та же CAS, что у reminders: два реактиватора не
            # разминутся
            grabbed = await s.scalar(
                text(
                    "UPDATE platform.activity_ledger SET state = 'running',"
                    " fencing_token = :fence, owner = :owner, attempts = attempts + 1"
                    " WHERE activity_id = :id AND state = 'failed' RETURNING activity_id"
                ).bindparams(id=activity_id[:240], fence=int(fencing_token), owner=self._owner())
            )
            if grabbed is not None:
                return None
        return ActivityState(
            activity_id=activity_id,
            state=state,
            fencing_token=int(row["fencing_token"] or 0),
            result=dict(result or {}),
        )

    async def complete(
        self, activity_id: str, result: dict[str, Any], *, fencing_token: int = 0
    ) -> None:
        sql = """
        UPDATE platform.activity_ledger
           SET state = 'done', result = CAST(:result AS jsonb), completed_at = now()
         WHERE activity_id = :id AND fencing_token = :fence
        """
        async with self._session() as s:
            await s.execute(
                text(sql).bindparams(
                    id=activity_id[:240],
                    result=orjson.dumps(dict(result)).decode(),
                    fence=int(fencing_token),
                )
            )
            await s.commit()

    async def fail(self, activity_id: str, error: str, *, fencing_token: int = 0) -> None:
        # строку НЕ удаляем: state='failed' + attempts — история «этот id уже пробовали», и
        # «упал на середине» должен отличаться от «никогда не начинал»
        sql = """
        UPDATE platform.activity_ledger
           SET state = 'failed', last_error = :err, attempts = attempts + 1
         WHERE activity_id = :id AND fencing_token = :fence
        """
        async with self._session() as s:
            await s.execute(
                text(sql).bindparams(
                    id=activity_id[:240], err=error[:2000], fence=int(fencing_token)
                )
            )
            await s.commit()

    async def compensate(self, activity_id: str, note: str = "") -> None:
        sql = """
        UPDATE platform.activity_ledger
           SET state = 'compensated', last_error = :note
         WHERE activity_id = :id
        """
        async with self._session() as s:
            await s.execute(text(sql).bindparams(id=activity_id[:240], note=note[:2000]))
            await s.commit()

    async def reap_expired(self, lease_s: int = 900) -> int:
        sql = """
        UPDATE platform.activity_ledger
           SET state = 'failed',
               last_error = coalesce(last_error, '') || ' | lease expired',
               attempts = attempts + 1
         WHERE state = 'running'
           AND started_at < now() - make_interval(secs => :secs)
        """
        async with self._session() as s:
            result = await s.execute(text(sql).bindparams(secs=int(lease_s)))
            await s.commit()
            return int(result.rowcount or 0)


async def run_once(
    ledger: Ledger,
    activity_id: str,
    fn: Callable[[], Awaitable[dict[str, Any]]],
    *,
    trace_id: str = "",
    fencing_token: int = 0,
) -> ActivityOutcome:
    """Исполнить эффект ровно один раз на activity_id; повтор получает сохранённый результат.

    Контракт для вызывающего: `fn` возвращает JSON-совместимый dict — то, что можно сохранить в
    реестре и отдать повторившему. Именно поэтому обёртка, а не «пусть activity сериализует
    что хочет»: Temporal сериализует возврат activity тем же способом, и общий формат делает
    локальный прогон и workflow-прогон взаимозаменяемыми (F8, ADR-0010).

    Исключение из `fn` = `fail`: строка становится терминальной, и следующий старт (после
    реакции человека или retry) заберёт её новым токеном — «зависший running» не блокирует
    повтор на века, ровно как в напоминаниях.
    """
    state = await ledger.start(activity_id, fencing_token=fencing_token, trace_id=trace_id)
    if state is not None:
        if state.state == "done":
            return ActivityOutcome("done", dict(state.result or {}), replayed=True)
        return ActivityOutcome(state.state, {}, replayed=False)
    try:
        value = await fn()
    except Exception as exc:
        await ledger.fail(
            activity_id, f"{type(exc).__name__}: {str(exc)[:500]}", fencing_token=fencing_token
        )
        raise
    payload = dict(value or {})
    await ledger.complete(activity_id, payload, fencing_token=fencing_token)
    return ActivityOutcome("done", payload, replayed=False)


def _is_uuid(value: Any) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError):
        return False
    return True
