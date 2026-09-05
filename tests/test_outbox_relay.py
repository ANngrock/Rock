"""Outbox-relay: субъекты, конверт, «что считать доставленным» и поведение при отказе транспорта.

NATS здесь подменён: `publish` — это одна строка SDK, а все решения (порядок, отметки, попытки,
остановка на отказе) — в `drain`, и они проверяются без брокера. Живой Postgres — в
`tests/integration/test_outbox_relay.py`.
"""

from __future__ import annotations

from typing import Any

import orjson
import pytest

from aegis.platform.events.relay import (
    RelayReport,
    drain,
    encode_event,
    subject_for,
)


def row(
    outbox_id: int, *, stream_type: str = "owner", stream_id: str = "owner:42"
) -> dict[str, Any]:
    return {
        "outbox_id": outbox_id,
        "event_id": 1000 + outbox_id,
        "stream_type": stream_type,
        "stream_id": stream_id,
        "version": outbox_id,
        "event_type": "conversation.turn_received",
        "payload": {"text": "привет"},
        "metadata": {"event_id": f"ev-{outbox_id}", "occurred_at": "2026-09-05T10:00:00+00:00"},
    }


class Store:
    """Магазин-двойник: помнит, что ему приносили, и различает `fetch`/`peek`."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        counts: dict[str, int] | None = None,
        counts_fail: bool = False,
        mark_fail: bool = False,
    ) -> None:
        self.rows = rows
        self._counts = counts or {}
        self._counts_fail = counts_fail
        self._mark_fail = mark_fail
        self.published: list[int] = []
        self.failures: list[tuple[int, str]] = []
        self.fetch_kwargs: list[dict[str, Any]] = []
        self.peeked = 0

    async def fetch_unpublished(
        self, limit: int = 100, *, max_attempts: int | None = None
    ) -> list[dict[str, Any]]:
        self.fetch_kwargs.append({"limit": limit, "max_attempts": max_attempts})
        return self.rows[:limit]

    async def peek_unpublished(
        self, limit: int = 100, *, max_attempts: int | None = None
    ) -> list[dict[str, Any]]:
        self.peeked += 1
        self.fetch_kwargs.append({"limit": limit, "max_attempts": max_attempts, "peek": True})
        return self.rows[:limit]

    async def mark_published(self, outbox_ids: list[int]) -> None:
        if self._mark_fail:
            raise RuntimeError("база легла")
        self.published.extend(outbox_ids)

    async def mark_failed(self, outbox_id: int, error: str) -> None:
        self.failures.append((outbox_id, error))

    async def counts(self, *, max_attempts: int | None = None) -> dict[str, int]:
        if self._counts_fail:
            raise OSError("connection refused")
        return dict(self._counts)


class Publisher:
    def __init__(self, *, fails_at: int | None = None, error: Exception | None = None) -> None:
        self.sent: list[tuple[str, bytes]] = []
        self.closed = 0
        self.fails_at = fails_at
        self.error = error or ConnectionError("nats: connection closed")

    async def publish(self, subject: str, body: bytes) -> None:
        if self.fails_at is not None and len(self.sent) + 1 >= self.fails_at:
            raise self.error
        self.sent.append((subject, body))

    async def aclose(self) -> None:
        self.closed += 1


# ------------------------------------------------------------------ субъекты и конверт


def test_subject_has_exactly_four_levels_after_sanitizing() -> None:
    subject = subject_for("owner", "owner:42", "conversation.turn_received")

    assert subject == "aegis.owner.owner-42.conversation-turn_received"
    assert len(subject.split(".")) == 4, "уровни субъекта — это контракт подписки `aegis.*.*.*`"


def test_subject_cannot_be_widened_by_event_type() -> None:
    """`event_type` приходит из БД; `x.>` внутри него превратил бы подписку в «слушать всё»."""
    subject = subject_for("owner", "1", "a.> .b")
    assert ">" not in subject and "*" not in subject
    assert subject == "aegis.owner.1.a-b"


def test_subject_honours_the_prefix() -> None:
    assert subject_for("owner", "1", "e", prefix="family").startswith("family.owner.1.")


def test_envelope_carries_dedup_keys_and_payload() -> None:
    body = orjson.loads(encode_event(row(7)))
    assert body["event_id"] == "ev-7", "по нему потребитель дедуплицирует повторную доставку"
    assert body["type"] == "conversation.turn_received"
    assert body["payload"] == {"text": "привет"}
    assert body["version"] == 7 and body["outbox_id"] == 7
    assert body["occurred_at"] == "2026-09-05T10:00:00+00:00"


def test_envelope_survives_a_row_without_metadata() -> None:
    raw = row(3)
    raw.pop("metadata")
    body = orjson.loads(encode_event(raw))
    assert body["event_id"] == "outbox-3", (
        "без event_id событие нечем дедуплицировать — шлём id строки"
    )
    assert body["metadata"] == {} and body["occurred_at"] is None
    assert body["payload"] == {"text": "привет"}, "тело события живёт отдельно от метаданных"


# ------------------------------------------------------------------ проход


async def test_drain_publishes_every_row_and_marks_once() -> None:
    store, pub = Store([row(1), row(2), row(3)], counts={"pending": 0, "stuck": 0}), Publisher()
    report = await drain(store, pub, limit=50, max_attempts=8)

    assert report.ok and report.published == 3 and report.fetched == 3
    expected = "aegis.owner.owner-42.conversation-turn_received"
    assert [subj for subj, _ in pub.sent] == [expected] * 3
    assert store.published == [1, 2, 3], "отметка одна на весь пакет, а не по строке"
    assert store.fetch_kwargs == [{"limit": 50, "max_attempts": 8}]


async def test_drain_on_empty_queue_says_so_without_publishing() -> None:
    pub = Publisher()
    report = await drain(Store([]), pub)
    assert report.ok and report.fetched == 0 and pub.sent == []
    assert "Неопубликованных событий нет" in report.summary()


async def test_transport_failure_keeps_the_rest_of_the_batch_queued() -> None:
    """Транспорт лёг на второй строке: первая опубликована и отмечена, остальные — нет.

    Повторять пакет до конца бессмысленно (получим N таймаутов вместо одного), а отмечать
    неопубликованное — значит потерять события. Отсюда и `break`, и `mark_failed` только по одной
    строке: её попытка действительно израсходована.
    """
    store = Store([row(1), row(2), row(3)], counts={"pending": 5, "stuck": 0})
    pub = Publisher(fails_at=2)
    report = await drain(store, pub, max_attempts=8)

    assert store.published == [1]
    assert report.published == 1 and report.failed == 2 and not report.ok
    assert "nats: connection closed" in (report.stopped or "")
    assert [i for i, _ in store.failures] == [2], "штрафуется только строка, которая не ушла"
    assert ConnectionError.__name__ in store.failures[0][1]
    assert len(pub.sent) == 1
    assert "не доставлено: 2" in report.summary()
    assert "ещё 5" in report.summary(), "очередь посчитана после тика: 5 остались"


async def test_dry_run_reads_with_peek_and_touches_nothing() -> None:
    store = Store([row(1), row(2)], counts={"pending": 2, "stuck": 0})
    pub = Publisher()
    report = await drain(store, pub, dry_run=True)

    assert store.peeked == 1 and store.fetch_kwargs[0]["peek"] is True
    assert store.published == [] and store.failures == []
    assert pub.sent == [], "--dry-run ничего не публикует"
    assert report.dry_run and report.fetched == 2 and report.ok
    text = report.summary()
    assert "2 события ушли бы в NATS" in text and "ничего не опубликовано" in text


async def test_stuck_rows_are_reported_and_left_alone() -> None:
    store = Store([], counts={"pending": 9, "stuck": 4})
    report = await drain(store, Publisher())
    assert report.stuck == 4 and report.fetched == 0
    assert "4 исчерпали попытки" in report.summary()
    assert report.ok is True, "пустой тик — не сбой: строки не исчезли, они ждут реакции"


async def test_missing_counters_do_not_break_the_run() -> None:
    """Магазин без `counts` (например, тестовый двойник) — это «не видно», а не «ноль»."""

    class Bare(Store):
        counts = None  # type: ignore[assignment]

    report = await drain(Bare([row(1)]), Publisher())
    assert report.published == 1 and report.pending == 0 and report.stuck == 0


async def test_counter_failure_is_silent_in_the_report_but_not_fatal() -> None:
    report = await drain(Store([row(1)], counts_fail=True), Publisher())
    assert report.published == 1 and report.pending == 0


async def test_mark_published_failure_is_visible_as_failure() -> None:
    """Отметка не записана = событие уйдёт повторно. Это at-least-once, и оно должно быть видно.

    Магазин бросает на `mark_published` — relay не обязан этого уметь чинить, но и молчать про
    «опубликовано 3» не должен: исключение идёт наружу, тик завершится кодом 1.
    """
    store = Store([row(1)], counts={"pending": 1, "stuck": 0}, mark_fail=True)

    with pytest.raises(RuntimeError, match="база легла"):
        await drain(store, Publisher())


# ------------------------------------------------------------------ отчёт


def test_report_summaries_read_as_a_diagnosis() -> None:
    assert "Неопубликованных событий нет" in RelayReport().summary()
    assert "Опубликовано 2 из 2" in RelayReport(published=2, fetched=2).summary()
    text = RelayReport(
        published=0, failed=3, fetched=3, pending=3, stopped="NATS не отвечает"
    ).summary()
    assert "не доставлено: 3" in text and "остановлено: NATS не отвечает" in text
    assert "4 события ушли бы" in RelayReport(fetched=4, dry_run=True).summary()
    assert "1 событие ушло бы" in RelayReport(fetched=1, dry_run=True).summary()
    assert "11 событий ушло бы" in RelayReport(fetched=11, dry_run=True).summary()


def test_transport_protocol_is_satisfied_by_the_double() -> None:
    """Двойник обязан подходить порту структурно — иначе тесты проверяют не то, что в проде."""
    from aegis.platform.events.relay import Transport

    assert isinstance(Publisher(), Transport)
