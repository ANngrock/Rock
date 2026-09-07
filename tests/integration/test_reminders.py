"""Напоминания на живом Postgres: переезд схемы, аренда строки, попытки, отмена.

Офлайн-тесты проверяют арифметику моментов; здесь — то, что мок увидеть не может: ``FOR UPDATE
SKIP LOCKED``, ``make_interval``, частичный индекс с новым предикатом и CHECK, который обязан
пускать ``sending`` и не пускать прочее. Этот слой и подводил тесты без БД.

Таблица на всех одна и чистки между тестами нет (это рабочее расписание, а не журнал), поэтому
каждый тест берёт своего ``owner_id`` и убирает за собой: иначе оставленная «scheduled + пора»
строка изменила бы результат следующего теста, и «падает только после X» стало бы нормой.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aegis.planning.reminders import MAX_ATTEMPTS, Reminder, SqlReminderStore, deliver
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
                await s.execute(text("SELECT 1 FROM planning.reminders LIMIT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"миграция 0004 не накатана ({type(exc).__name__}) — `make migrate`")
        yield
        reset_engine()


@pytest_asyncio.fixture
async def owner(db: None) -> AsyncIterator[int]:
    """Чужой владелец + уборка его незакрытых строк после теста."""
    value = int(uuid.uuid4().int % 2_000_000_000) + 10_000
    try:
        yield value
    finally:
        async with session() as s:
            await s.execute(
                text(
                    "UPDATE planning.reminders SET status = 'cancelled'"
                    " WHERE owner_id = :owner_id AND status IN ('scheduled', 'sending')"
                ),
                {"owner_id": value},
            )
            await s.commit()


async def _rows(owner_id: int) -> list[dict[str, Any]]:
    async with session() as s:
        result = await s.execute(
            text(
                "SELECT id::text AS id, text, status, attempts, due_at, sent_at, last_error"
                " FROM planning.reminders WHERE owner_id = :owner_id ORDER BY id"
            ),
            {"owner_id": owner_id},
        )
        return [dict(row) for row in result.mappings().all()]


async def _row(owner_id: int, reminder_id: str) -> dict[str, Any]:
    rows = {row["id"]: row for row in await _rows(owner_id)}
    return rows[reminder_id]


async def _due_reminder(owner_id: int, store: SqlReminderStore, body: str) -> str:
    """Напоминание, у которого срок уже наступил (именно такое и забирает тик)."""
    return await store.add(
        owner_id=owner_id, body=body, due_at=datetime.now(UTC) - timedelta(minutes=5)
    )


async def _claim_all(store: SqlReminderStore) -> list[Reminder]:
    return await store.claim_due(limit=500)


# ------------------------------------------------------------- переезд --


@pytest.mark.usefixtures("db")
async def test_table_moved_out_of_governance() -> None:
    async with session() as s:
        where = (
            (
                await s.execute(
                    text(
                        "SELECT table_schema FROM information_schema.tables"
                        " WHERE table_name = 'reminders'"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert "planning" in where, "напоминания должны жить в planning"
    assert "governance" not in where, "старая схема не имеет права оставлять копию таблицы"


@pytest.mark.usefixtures("owner")
async def test_lease_status_is_allowed_and_garbage_is_not(owner: int) -> None:
    """CHECK из 0004 обязан пускать ``sending`` (аренда) и не пускать выдуманные статусы."""
    due = datetime.now(UTC) - timedelta(minutes=1)
    async with session() as s:
        await s.execute(
            text(
                "INSERT INTO planning.reminders (owner_id, text, due_at, status)"
                " VALUES (:owner_id, :text, :due_at, 'sending')"
            ),
            {"owner_id": owner, "text": "аренда", "due_at": due},
        )
        await s.commit()
    with pytest.raises(DBAPIError):
        async with session() as s:
            await s.execute(
                text(
                    "INSERT INTO planning.reminders (owner_id, text, due_at, status)"
                    " VALUES (:owner_id, :text, :due_at, 'paused')"
                ),
                {"owner_id": owner, "text": "мусор", "due_at": due},
            )


@pytest.mark.usefixtures("db")
async def test_partial_index_covers_the_lease() -> None:
    """Индекс тика обязан совпадать с его предикатом: иначе каждый проход — полное сканирование."""
    async with session() as s:
        definition = (
            await s.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'reminders_due'")
            )
        ).scalar()
    assert definition and "sending" in definition, f"индекс не покрывает аренду: {definition}"


# --------------------------------------------------------------- обещания --


@pytest.mark.usefixtures("owner")
async def test_add_list_cancel_round_trip(owner: int) -> None:
    store = SqlReminderStore()
    moment = datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(hours=2)
    reminder_id = await store.add(owner_id=owner, body="  позвонить   в банк  ", due_at=moment)

    items = await store.list_scheduled(owner_id=owner)
    assert [item.text for item in items] == ["позвонить в банк"], "пробелы вычищаются при записи"
    # timestamptz хранит момент, а не «строку времени»: пояс владельца обязан пережить запись
    assert items[0].due_at == moment

    removed = await store.cancel(owner_id=owner, ref="банк")
    assert removed is not None and removed.status == "cancelled"
    assert await store.list_scheduled(owner_id=owner) == []
    assert (await _row(owner, reminder_id))["status"] == "cancelled"
    assert await store.cancel(owner_id=owner, ref=reminder_id[:8]) is None, "уже отменено"


@pytest.mark.usefixtures("owner")
async def test_cancel_does_not_touch_other_owners(owner: int) -> None:
    other = _other_owner()
    store = SqlReminderStore()
    due = datetime.now(UTC) + timedelta(hours=1)
    await store.add(owner_id=owner, body="моё", due_at=due)
    await store.add(owner_id=other, body="моё", due_at=due)
    assert await store.cancel(owner_id=owner, ref="моё") is not None
    assert len(await store.list_scheduled(owner_id=other)) == 1
    async with session() as s:
        await s.execute(
            text("UPDATE planning.reminders SET status = 'cancelled' WHERE owner_id = :o"),
            {"o": other},
        )
        await s.commit()


@pytest.mark.usefixtures("owner")
async def test_empty_text_is_rejected_by_the_database(owner: int) -> None:
    """Пустое напоминание — это «в 9:00 придёт пустота»; ограничение длины должно держаться."""
    store = SqlReminderStore()
    with pytest.raises(DBAPIError):
        await store.add(owner_id=owner, body="   ", due_at=datetime.now(UTC) + timedelta(hours=1))


# ------------------------------------------------------------------ тик --


@pytest.mark.usefixtures("owner")
async def test_only_due_rows_are_claimed_and_a_second_tick_sees_nothing(owner: int) -> None:
    store = SqlReminderStore()
    due_id = await _due_reminder(owner, store, "пора")
    await store.add(
        owner_id=owner, body="ещё не пора", due_at=datetime.now(UTC) + timedelta(hours=1)
    )

    claimed = await _claim_all(store)
    mine = [r for r in claimed if r.owner_id == owner]
    assert [r.id for r in mine] == [due_id], "будущее напоминание тик трогать не должен"
    assert mine[0].attempts == 1, "попытка считается при взятии, а не после отправки"
    assert due_id not in [r.id for r in await _claim_all(store)], (
        "арендованная строка не для второго прохода"
    )
    row = await _row(owner, due_id)
    assert row["status"] == "sending"
    assert row["sent_at"] is None, "пока не отправлено — sent_at обязан быть пустым"


@pytest.mark.usefixtures("owner")
async def test_failure_releases_the_lease_and_keeps_the_reason(owner: int) -> None:
    store = SqlReminderStore()
    due_id = await _due_reminder(owner, store, "сбойный")
    assert due_id in [r.id for r in await _claim_all(store)]
    await store.mark_failed(due_id, "telegram: 429 too many requests")

    row = await _row(owner, due_id)
    assert row["status"] == "scheduled", "не доставленное обязано вернуться в расписание"
    assert "429" in row["last_error"]
    again = [r for r in await _claim_all(store) if r.id == due_id]
    assert again and again[0].attempts == 2


@pytest.mark.usefixtures("owner")
async def test_attempts_ceiling_stops_the_row(owner: int) -> None:
    store = SqlReminderStore()
    due_id = await _due_reminder(owner, store, "упорный")
    for attempt in range(MAX_ATTEMPTS + 2):
        claimed = [r for r in await _claim_all(store) if r.id == due_id]
        if not claimed:
            break
        await store.mark_failed(due_id, f"сбой {attempt}")
    row = await _row(owner, due_id)
    assert row["status"] == "failed", "после потолка — статус, а не вечный цикл"
    assert row["attempts"] == MAX_ATTEMPTS
    assert due_id not in [r.id for r in await _claim_all(store)]


@pytest.mark.usefixtures("owner")
async def test_stale_lease_is_reclaimed(owner: int) -> None:
    """Убитый процесс не имеет права съедать напоминание: просроченная аренда вернётся в игру."""
    store = SqlReminderStore()
    due_id = await _due_reminder(owner, store, "разбившийся тик")
    assert due_id in [r.id for r in await _claim_all(store)]
    assert due_id not in [r.id for r in await _claim_all(store)]
    async with session() as s:
        await s.execute(
            text(
                "UPDATE planning.reminders SET updated_at = now() - interval '20 minutes'"
                " WHERE id = :id"
            ),
            {"id": due_id},
        )
        await s.commit()
    assert due_id in [r.id for r in await _claim_all(store)], "аренда протухла — строка снова пора"


@pytest.mark.usefixtures("owner")
async def test_concurrent_ticks_never_share_a_reminder(owner: int) -> None:
    """Два тика одновременно: SKIP LOCKED обязан развести строки, а не удвоить сообщения."""
    store = SqlReminderStore()
    ids = {(await _due_reminder(owner, store, f"пачка {n}")) for n in range(4)}
    left, right = await asyncio.gather(_claim_all(store), _claim_all(store))
    mine = [r for r in left + right if r.owner_id == owner]
    assert {r.id for r in mine} == ids, "каждая строка взята ровно один раз"
    assert len(mine) == len({r.id for r in mine}), "пересечения двух тиков быть не может"


@pytest.mark.usefixtures("owner")
async def test_deliver_marks_sent(owner: int) -> None:
    store = SqlReminderStore()
    due_id = await _due_reminder(owner, store, "доставить")
    sent: list[str] = []

    async def send(reminder: Reminder) -> None:
        sent.append(reminder.text)

    report = await deliver(store, send=send)
    assert "доставить" in sent
    # отчёт говорит короткими id — это то, что человек читает в выводе тика
    assert due_id[:8] in report.sent
    row = await _row(owner, due_id)
    assert row["status"] == "sent" and row["sent_at"] is not None
    assert await store.list_scheduled(owner_id=owner) == []


@pytest.mark.usefixtures("owner")
async def test_peek_does_not_change_anything(owner: int) -> None:
    """``--dry-run`` показывает пора, не забирая его: прогон «просто посмотреть» не имеет права
    сдвигать попытки или гасить напоминание."""
    store = SqlReminderStore()
    due_id = await _due_reminder(owner, store, "сухая проба")
    assert due_id in [r.id for r in await store.peek_due(limit=500)]
    row = await _row(owner, due_id)
    assert row["status"] == "scheduled" and row["attempts"] == 0


@pytest.mark.usefixtures("owner")
async def test_counts_report_the_schedule_state(owner: int) -> None:
    store = SqlReminderStore()
    await _due_reminder(owner, store, "просрочено")
    counts = await store.counts()
    assert set(counts) == {"scheduled", "overdue", "sent", "failed"}
    assert counts["overdue"] >= 1


def _other_owner() -> int:
    return int(uuid.uuid4().int % 2_000_000_000) + 10_000


# --------------------------------------------------------------- каналы --


@pytest.mark.usefixtures("db")
async def test_channel_round_trip_and_database_check(owner: int) -> None:
    """Канал переживает запись и выдачу, а CHECK держит мусор даже на прямом UPDATE.

    Констрейнт проверяем именно здесь: в офлайн-тестах он есть только как строка в миграции,
    а «копья не пущены, потому что их в БД не проверяют» — классический способ разъехаться
    коду и схеме.
    """
    store = SqlReminderStore()
    due = datetime.now(UTC) + timedelta(hours=1)
    call_id = await store.add(owner_id=owner, body="позвонить маме", due_at=due, channel="call")
    plain_id = await store.add(owner_id=owner, body="обычное", due_at=due)

    items = {item.id[:8]: item for item in await store.list_scheduled(owner_id=owner)}
    assert items[call_id[:8]].channel == "call"
    assert items[plain_id[:8]].channel == "message", (
        "DEFAULT 'message' — старые строки не осиротели"
    )

    async with session() as s:
        with pytest.raises(DBAPIError, match="reminders_channel_chk"):
            await s.execute(
                text("UPDATE planning.reminders SET channel = 'sms' WHERE id = :i"),
                {"i": call_id},
            )
        await s.rollback()


@pytest.mark.usefixtures("db")
async def test_channel_column_has_honest_default() -> None:
    """DEFAULT 'message' живёт в схеме, а не только в коде."""
    async with session() as s:
        default = (
            await s.execute(
                text(
                    "SELECT column_default FROM information_schema.columns"
                    " WHERE table_schema = 'planning' AND table_name = 'reminders'"
                    " AND column_name = 'channel'"
                )
            )
        ).scalar()
    assert default is not None and "message" in str(default), (
        "миграция 0009 не накатана — `aegis migrate`/`make migrate`"
    )


@pytest.mark.usefixtures("db")
async def test_failed_call_still_delivers_and_closes_the_row(owner: int) -> None:
    """Звонок упал — напоминание доставлено текстом и строка ушла в sent, а не в бесконечный retry.

    Это сценарий «немого обещания» наоборот: провайдер живёт своей жизнью (абонент недоступен,
    лимиты, сеть), и единственный недопустимый исход — когда владелец не узнал НИ-ЧТО.
    """
    from aegis.interaction.calls import CallError
    from aegis.interaction.notify import ReminderDispatcher

    store = SqlReminderStore()
    due_id = await store.add(
        owner_id=owner,
        body="записать ребёнка к врачу",
        due_at=datetime.now(UTC) - timedelta(minutes=5),
        channel="call",
    )

    class _FakeTelegram:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str | None]] = []

        async def send(self, reminder: Reminder, prefix: str | None = None) -> None:
            self.sent.append((str(reminder.text), prefix))

        async def start(self) -> None:
            pass

        async def aclose(self) -> None:
            pass

    class _BusyProvider:
        async def call(self, to: str, text: str) -> None:
            raise CallError("all lines busy")

    telegram = _FakeTelegram()
    dispatcher = ReminderDispatcher(
        telegram=telegram, calls=_BusyProvider(), phone="+70000000000", timezone="UTC"
    )
    report = await deliver(store, send=dispatcher.send)

    assert due_id[:8] in report.sent, "fallback-доставка = успешная доставка"
    row = await _row(owner, due_id)
    assert row["status"] == "sent" and row["last_error"] is None
    confession = [item for item in telegram.sent if item[1] and "Не дозвонился" in item[1]]
    assert confession, "об отказе дозвониться владелец узнаёт из того же сообщения"
    assert any("звонок не удался" in note for note in dispatcher.last_notes)
