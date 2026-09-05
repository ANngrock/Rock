"""Outbox-relay на живом Postgres: выборка с блокировкой, попытки, «доставлено» после ack.

Офлайн-тест проверяет арифметику прохода на двойнике. Здесь — то, что двойник не увидит: `FOR UPDATE
OF o SKIP LOCKED` с сетевой паузой посередине, `attempts`/`last_error`, частичный индекс
`outbox_unpublished_idx` и то, что `CAST(:max_attempts AS int)` вообще принимает asyncpg (с `::int`
он падал на живом драйвере, и офлайн это не поймать).

Таблица на всех одна, поэтому каждый тест работает со своим `stream_id` и проверяет свои строки:
«падает только после X» — самый дорогой класс отладки на общем расписании.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import orjson
import pytest
import pytest_asyncio
from sqlalchemy import text

from aegis.platform.config import override_settings
from aegis.platform.db import reset_engine, session
from aegis.platform.events.relay import drain
from aegis.platform.events.store import EventStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("AEGIS_TEST_DATABASE_URL"), reason="нужен Postgres"),
]


@pytest_asyncio.fixture
async def db() -> AsyncIterator[str]:
    """Готовая база и уникальный id потока для теста."""
    url = os.environ["AEGIS_TEST_DATABASE_URL"]
    stream_id = f"owner:{uuid.uuid4().hex[:10]}"
    with override_settings(database_url=url):
        reset_engine()
        try:
            async with session() as s:
                await s.execute(text("SELECT 1 FROM platform.outbox LIMIT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"схема events не накатана ({type(exc).__name__}) — `make migrate`")
        yield stream_id
        # очистки нет и она невозможна: platform.events — append-only (триггеры миграции 0001
        # запрещают UPDATE/DELETE намеренно). Поэтому у каждого теста свой stream_id и проверки
        # идут по своим строкам, а не «сколько всего в таблице».
        reset_engine()


class Publisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, bytes]] = []
        self.closed = 0
        self.fail = fail

    async def publish(self, subject: str, body: bytes) -> None:
        if self.fail:
            raise ConnectionError("nats: connection closed")
        self.sent.append((subject, body))

    async def aclose(self) -> None:
        self.closed += 1


async def append(*, stream_id: str, event_type: str = "conversation.turn_received") -> int:
    async with session() as s:
        return await EventStore(s).append(
            stream_type="owner",
            stream_id=stream_id,
            event_type=event_type,
            payload={"text": "привет, мир"},
        )


async def rows_for(stream_id: str) -> list[dict[str, Any]]:
    async with session() as s:
        result = await s.execute(
            text(
                """
                SELECT o.id, o.published_at IS NOT NULL, o.attempts, o.last_error
                FROM platform.outbox o
                JOIN platform.events e ON e.id = o.event_id
                WHERE e.stream_id = :sid
                ORDER BY o.id
                """
            ).bindparams(sid=stream_id)
        )
        return [
            {"id": r[0], "published": bool(r[1]), "attempts": int(r[2]), "error": r[3]}
            for r in result
        ]


async def test_published_flag_and_counters_round_trip(db: str) -> None:
    stream_id = db
    for _ in range(3):
        await append(stream_id=stream_id)
    assert all(r["published"] is False for r in await rows_for(stream_id))

    pub = Publisher()
    async with session() as s:
        report = await drain(EventStore(s), pub, limit=500, max_attempts=8)
    assert report.ok and report.published >= 3
    assert len(pub.sent) == report.published
    mine = await rows_for(stream_id)
    assert len(mine) == 3 and all(r["published"] for r in mine), "отметка обязана дойти до строк"
    assert all(r["attempts"] == 0 for r in mine), "успех не должен тратить попытки"

    subjects = {s for s, _ in pub.sent}
    assert len(subjects) == 1 and next(iter(subjects)).count(".") == 3
    body = orjson.loads(pub.sent[0][1])
    assert body["payload"] == {"text": "привет, мир"}, "кириллица проходит через jsonb целой"
    assert body["stream_id"] == stream_id


async def test_second_drain_finds_nothing(db: str) -> None:
    await append(stream_id=db)
    async with session() as s:
        await drain(EventStore(s), Publisher(), limit=500)
    pub = Publisher()
    async with session() as s:
        report = await drain(EventStore(s), pub, limit=500)
    # чужие строки в этой таблице — не наш сюжет: проверяем, что СВОЁ не уехало повторно
    assert db.replace(":", "-") not in " ".join(subject for subject, _ in pub.sent)
    assert all(r["published"] for r in await rows_for(db))
    assert report.ok, "второй проход не имеет права ничего портить: ни строк, ни попыток"


async def test_failed_publish_burns_one_attempt_and_keeps_the_row(db: str) -> None:
    await append(stream_id=db)
    async with session() as s:
        report = await drain(EventStore(s), Publisher(fail=True), limit=500, max_attempts=8)

    assert report.published == 0 and report.failed >= 1 and not report.ok
    mine = await rows_for(db)
    assert mine and mine[0]["published"] is False
    assert mine[0]["attempts"] == 1, "попытка тратится только на строку, которая не ушла"
    assert "ConnectionError" in (mine[0]["error"] or "")


async def test_exhausted_attempts_leave_the_queue_selection(db: str) -> None:
    """`max_attempts` работает именно в выборке: «ядовитая» строка не вытесняет нормальные."""
    await append(stream_id=db)
    async with session() as s:
        await drain(EventStore(s), Publisher(fail=True), limit=500, max_attempts=1)
    assert (await rows_for(db))[0]["attempts"] == 1

    fresh = await append(stream_id=db, event_type="conversation.turn_answered")
    pub = Publisher()
    async with session() as s:
        report = await drain(EventStore(s), pub, limit=500, max_attempts=1)
    mine = await rows_for(db)
    stuck, ok_row = mine[0], mine[1]
    assert ok_row["published"] is True and stuck["published"] is False
    assert fresh == ok_row["id"], "новая строка прошла, застрявшая — мимо выборки"
    assert report.stuck >= 1

    counts = await _counts(max_attempts=1)
    assert counts["stuck"] >= 1 and counts["pending"] >= 1


async def test_dry_run_changes_nothing_at_all(db: str) -> None:
    await append(stream_id=db)
    pub = Publisher()
    async with session() as s:
        report = await drain(EventStore(s), pub, limit=500, dry_run=True)
    mine = await rows_for(db)
    assert pub.sent == [], "проба не имеет права трогать брокер"
    assert report.fetched >= 1 and report.published == 0
    assert mine[0]["published"] is False and mine[0]["attempts"] == 0


async def test_lock_holds_the_row_for_the_whole_publish(db: str) -> None:
    """Пока один relay держит строку, второй обязан её пропустить, а не публиковать дважды.

    Пауза между `fetch` и `commit` — это и есть время сетевого вызова: если бы блокировка
    снималась раньше, два тика могли бы гнать одно событие параллельно.
    """
    await append(stream_id=db)
    async with session() as first:
        store = EventStore(first)
        locked = await store.fetch_unpublished(limit=500, max_attempts=8)
        assert locked
        async with session() as second:
            other = await EventStore(second).fetch_unpublished(limit=500, max_attempts=8)
        assert all(r["outbox_id"] != locked[0]["outbox_id"] for r in other), (
            "строка ушла из-под носа: SKIP LOCKED не сработал"
        )


async def _counts(*, max_attempts: int) -> dict[str, int]:
    async with session() as s:
        return await EventStore(s).counts(max_attempts=max_attempts)
