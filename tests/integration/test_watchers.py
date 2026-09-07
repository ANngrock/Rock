"""Наблюдатели на живом Postgres: атомарность срабатывания, аренда, ограничения, горизонт.

Главный контракт здесь — «условие сожжено и напоминание создано» как один коммит: если он
рассыпается на два, перезапуск тика между ними крадёт у владельца весть (условие-то уже учтено).
Всё остальное — CHECK'и и SKIP LOCKED, которые мок не воспроизводит никогда.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aegis.planning.watchers import (
    SqlWatchStore,
    Watch,
    WatchReport,
    run_watches,
)
from aegis.platform.config import override_settings
from aegis.platform.db import reset_engine, session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("AEGIS_TEST_DATABASE_URL"), reason="нужен Postgres"),
]


@pytest_asyncio.fixture
async def db() -> AsyncIterator[None]:
    url = os.environ["AEGIS_TEST_DATABASE_URL"]
    with override_settings(database_url=url):
        reset_engine()
        try:
            async with session() as s:
                await s.execute(text("SELECT 1 FROM planning.watches LIMIT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"миграция 0010 не накатана ({type(exc).__name__}) — `make migrate`")
        yield
        reset_engine()


@pytest_asyncio.fixture
async def owner(db: None) -> AsyncIterator[int]:
    value = int(uuid.uuid4().int % 2_000_000_000) + 10_000
    try:
        yield value
    finally:
        async with session() as s:
            await s.execute(text("DELETE FROM planning.watches WHERE owner_id = :o"), {"o": value})
            await s.execute(
                text("DELETE FROM planning.reminders WHERE owner_id = :o"), {"o": value}
            )
            await s.commit()


_OPEN_SQL = "UPDATE planning.watches SET fire_at = now() - interval '1 minute' WHERE owner_id = :o"


async def _open_gate(owner_id: int) -> None:
    """add() всегда ставит fire_at=+1 мин; тесту надо «созрело» — сдвигаем прямой константой."""
    async with session() as s:
        await s.execute(text(_OPEN_SQL), {"o": owner_id})
        await s.commit()


class _FakeChecker:
    """WatchChecker без сети: протокол — один метод body(watch) -> str."""

    def __init__(
        self, body: str = "цена 3 400 рублей; скидка!", *, error: str | None = None
    ) -> None:
        self.payload = body
        self.error = error

    async def body(self, watch: Watch) -> str:
        if self.error:
            from aegis.planning.watchers import CheckError

            raise CheckError(self.error)
        return self.payload


@pytest.mark.usefixtures("db")
async def test_add_list_cancel_round_trip(owner: int) -> None:
    store = SqlWatchStore()
    watch_id = await store.add(
        owner_id=owner,
        title="билеты до берлина",
        kind="page",
        target="https://example.test/t",
        mode="contains",
        needle="3 400",
        interval_minutes=10,
    )
    rows = await store.list_active(owner_id=owner)
    assert [r["id"][:8] for r in rows] == [watch_id[:8]]
    assert rows[0]["status"] == "active"

    outcome = await store.set_status(owner_id=owner, ref="берлин", status="cancel")
    assert outcome is not None and await store.list_active(owner_id=owner) == []

    # чужой наблюдатель не отменяется словом (тот же RLS-дух, что у напоминаний)
    other = int(uuid.uuid4().int % 2_000_000_000) + 10_000
    await store.add(
        owner_id=other,
        title="билеты до берлина",
        kind="page",
        target="https://example.test/t",
        needle="x",
    )
    assert await store.set_status(owner_id=owner, ref="берлин", status="cancel") is None
    async with session() as s:
        await s.execute(text("DELETE FROM planning.watches WHERE owner_id = :o"), {"o": other})
        await s.commit()


@pytest.mark.usefixtures("db")
async def test_database_constraints_hold_garbage_out(owner: int) -> None:
    store = SqlWatchStore()
    with pytest.raises(ValueError):
        await store.add(
            owner_id=owner, title="x", kind="page", target="https://e.test", needle="a", mode="sms"
        )
    watch_id = await store.add(
        owner_id=owner, title="уровень воды", kind="page", target="https://e.test", needle="в"
    )
    async with session() as s:
        with pytest.raises(DBAPIError, match="watches_interval"):
            await s.execute(
                text("UPDATE planning.watches SET interval_minutes = 1 WHERE id = :i"),
                {"i": watch_id},
            )
        await s.rollback()
        # игла не нужна только у changed — прямая вставка без неё ловит выраженный CHECK
        with pytest.raises(DBAPIError, match="watches_needle_required"):
            await s.execute(
                text(
                    "INSERT INTO planning.watches (owner_id, title, kind, target, mode,"
                    " interval_minutes) VALUES (:o, 'abc', 'page', 'https://e.test',"
                    " 'contains', 15)"
                ),
                {"o": owner},
            )
        await s.rollback()


@pytest.mark.usefixtures("db")
async def test_fire_creates_reminder_atomically(owner: int) -> None:
    """Попадание: одно предложение — переворот строки + вставка напоминания тем же каналом."""
    store = SqlWatchStore()
    await store.add(
        owner_id=owner,
        title="оферта до 3400",
        kind="page",
        target="https://example.test/t",
        needle="3 400",
        channel="call",
        repeat=False,
    )
    # досрочно открываем fire_at: add всегда ставит +1 минуту — тик проверяет «созрело»
    await _open_gate(owner)

    checker = _FakeChecker()
    report = await run_watches(store, page=checker)  # type: ignore[arg-type]
    assert report.fired == 1
    async with session() as s:
        row = (
            (
                await s.execute(
                    text(
                        "SELECT r.text, r.channel, r.status, w.status AS wstatus"
                        " FROM planning.reminders r, planning.watches w"
                        " WHERE r.owner_id = :o AND w.owner_id = :o"
                    ),
                    {"o": owner},
                )
            )
            .mappings()
            .one()
        )
    assert row["channel"] == "call" and row["status"] == "scheduled"
    assert row["wstatus"] == "fired", "one-shot погас — второго выстрела не будет"
    assert "оферта до 3400" in row["text"] and "найдено" in row["text"]


@pytest.mark.usefixtures("db")
async def test_miss_rearms_and_error_pauses(owner: int) -> None:
    store = SqlWatchStore()
    await store.add(
        owner_id=owner,
        title="ждём слово",
        kind="page",
        target="https://example.test/t",
        needle="никакого-совпадения",
        interval_minutes=5,
    )
    await _open_gate(owner)

    good = _FakeChecker(body="тут про другое")
    report = await run_watches(store, page=good)  # type: ignore[arg-type]
    assert report.checked == 1 and report.fired == 0 and report.rearmed == 1
    async with session() as s:
        fire = (
            await s.execute(
                text("SELECT fire_at > now() AS future FROM planning.watches WHERE owner_id = :o"),
                {"o": owner},
            )
        ).scalar()
    assert fire, "мимо — арм на интервал вперёд, не back-to-back"

    # 10 падений подряд = пауза; счётчик ведёт БД, цикл его только читает
    for _ in range(10):
        bad = _FakeChecker(error="connect timeout")
        await _open_gate(owner)
        await run_watches(store, page=bad)  # type: ignore[arg-type]
    rows = await store.list_active(owner_id=owner)
    assert rows and rows[0]["status"] == "paused"
    assert "connect timeout" in str(rows[0]["last_error"])
    resume = await store.set_status(owner_id=owner, ref="ждём", status="resume")
    assert resume is not None
    rows = await store.list_active(owner_id=owner)
    assert rows[0]["status"] == "active" and rows[0]["failures"] == 0


@pytest.mark.usefixtures("db")
async def test_concurrent_ticks_share_nothing(owner: int) -> None:
    """Два тика, один наблюдатель: проверку проходит кто-то один — второй не видит и не сдвигает."""
    store = SqlWatchStore()
    await store.add(
        owner_id=owner,
        title="лотерея",
        kind="page",
        target="https://example.test/t",
        needle="розыгрыш",
    )
    await _open_gate(owner)

    seen: list[int] = []

    class _SlowChecker(_FakeChecker):
        def __init__(self) -> None:
            super().__init__(body="завтра розыгрыш призов")

        async def body(self, watch: Watch) -> str:
            await asyncio.sleep(0.05)
            return self.payload

    async def tick() -> WatchReport:
        checker = _SlowChecker()
        # фиксируем число реальных захватов: claim устроен внутри run_watches, поэтому
        # второй concurrent-тик обязан получить checked=0 (SKIP LOCKED), а не тот же ряд
        seen.append(0)
        return await run_watches(store, page=checker)  # type: ignore[arg-type]

    r1, r2 = await asyncio.gather(tick(), tick())
    assert r1.checked + r2.checked == 1, "ряд выдан ровно одному тикеру"
    assert (r1.fired + r2.fired) == 1


@pytest.mark.usefixtures("db")
async def test_expiry_horizon_fires_nobody_past_it(owner: int) -> None:
    store = SqlWatchStore()
    watch_id = await store.add(
        owner_id=owner,
        title="последний шанс",
        kind="page",
        target="https://example.test/t",
        needle="x",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    async with session() as s:
        await s.execute(
            text(
                "UPDATE planning.watches SET fire_at = now() - interval '1 minute',"
                " expires_at = now() - interval '1 second' WHERE id = :i"
            ),
            {"i": watch_id},
        )
        await s.commit()
    report = await run_watches(store, page=_FakeChecker(body="тут x есть"))  # type: ignore[arg-type]
    assert report.checked == 0 and report.expired == 1  # expire — внутри claim_due
    async with session() as s:
        status = (
            await s.execute(
                text("SELECT status FROM planning.watches WHERE id = :i"), {"i": watch_id}
            )
        ).scalar()
    assert status == "expired"
