"""Напоминания вне базы: инструменты, отчёт тика и обещания магазина.

SQL проверяется в ``tests/integration/test_reminders.py``; здесь — то, что можно испортить без
постановки: что инструмент честно отказывает, когда хранить некуда; что текст ответа совпадает с
разобранным моментом, а не с тем, что нафантазировала модель; что сбой доставки не съедает
напоминание и не превращается в «отправлено».
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from aegis.agents.services import Services
from aegis.agents.supervisor import _reminders_status
from aegis.agents.tools import reminders as tools
from aegis.agents.tools.registry import ToolContext
from aegis.planning.reminders import (
    DELIVER_PREFIX,
    MAX_ATTEMPTS,
    DeliverReport,
    NullReminderStore,
    Reminder,
    SqlReminderStore,
    deliver,
)
from aegis.platform.config import Settings, override_settings


class Store:
    """Двойник расписания: помнит всё, что в него положили, и умеет подводить на каждом шаге."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        fail_add: Exception | None = None,
        scheduled: list[Reminder] | None = None,
        due: list[Reminder] | None = None,
    ) -> None:
        self.enabled = enabled
        self.fail_add = fail_add
        self.added: list[dict[str, Any]] = []
        self.scheduled = scheduled or []
        self.due = due or []
        self.sent: list[str] = []
        self.errors: list[tuple[str, str]] = []
        self.cancelled: list[str] = []

    async def add(
        self,
        *,
        owner_id: int,
        body: str,
        due_at: datetime,
        trace_id: str | None = None,
        channel: str = "message",
    ) -> str:
        if self.fail_add is not None:
            raise self.fail_add
        self.added.append(
            {"owner_id": owner_id, "body": body, "due_at": due_at, "trace_id": trace_id}
        )
        return f"{abs(hash(body + due_at.isoformat())):032x}"

    async def list_scheduled(self, *, owner_id: int, limit: int = 10) -> list[Reminder]:
        return self.scheduled[:limit]

    async def cancel(self, *, owner_id: int, ref: str) -> Reminder | None:
        for item in self.scheduled:
            if item.short_id.startswith(ref) or ref.lower() in item.text.lower():
                self.cancelled.append(item.id)
                return item
        return None

    async def claim_due(self, *, limit: int = 20) -> list[Reminder]:
        return self.due[:limit]

    async def peek_due(self, *, limit: int = 20) -> list[Reminder]:
        return self.due[:limit]

    async def mark_sent(self, reminder_id: str) -> None:
        self.sent.append(reminder_id)

    async def mark_failed(self, reminder_id: str, error: str) -> None:
        self.errors.append((reminder_id, error))


def ctx(store: Any) -> ToolContext:
    services = Services(gateway=None, facts=None, notes=None, reminders=store)  # type: ignore[arg-type]
    return ToolContext(trace_id="trace-1", owner_id=1, services=services)


def reminder(**kw: Any) -> Reminder:
    base = {
        "id": "6f2c9a1b-0000-4000-8000-000000000001",
        "text": "позвонить в банк",
        "due_at": datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
    }
    base.update(kw)
    return Reminder(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------- инструменты --


async def test_set_reminder_stores_the_parsed_moment_not_the_model_words() -> None:
    store = Store()
    with override_settings(timezone="Europe/Moscow", reminders_enabled=True):
        out = await tools.set_reminder(
            tools.SetReminderArgs(text="позвонить в банк", when="через 20 минут"), ctx(store)
        )
    assert len(store.added) == 1
    due_at = store.added[0]["due_at"]
    assert due_at.tzinfo is not None, "в расписание кладётся только absolute момент"
    assert abs((due_at - datetime.now(UTC)).total_seconds() - 20 * 60) < 120
    assert store.added[0]["owner_id"] == 1
    assert store.added[0]["trace_id"] == "trace-1"
    # ответ показывает ровно тот момент, который лёг в базу, — не пересказ слов владельца
    echo = due_at.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m %H:%M")
    assert f"Поставлено на {echo}" in out and "через" in out
    assert "cancel_reminder" in out, "ответ обязан говорить, как это отменить"


async def test_unparsable_time_is_refused_without_writing_anything() -> None:
    store = Store()
    with override_settings(timezone="Europe/Moscow", reminders_enabled=True):
        out = await tools.set_reminder(
            tools.SetReminderArgs(text="не знаю когда", when="когда-нибудь"), ctx(store)
        )
    assert store.added == []
    assert "Не ставлю" in out and "не понимаю" in out


async def test_default_hour_is_reported_to_the_owner_not_hidden() -> None:
    store = Store()
    with override_settings(timezone="Europe/Moscow", reminders_enabled=True):
        out = await tools.set_reminder(
            tools.SetReminderArgs(text="оплатить интернет", when="завтра"), ctx(store)
        )
    assert "время взято" in out, "придуманное парсером время обязано быть сказано в ответе"


async def test_disabled_feature_refuses_instead_of_losing_the_request() -> None:
    store = Store()
    with override_settings(timezone="Europe/Moscow", reminders_enabled=False):
        out = await tools.set_reminder(
            tools.SetReminderArgs(text="звёзды", when="через час"), ctx(store)
        )
    assert store.added == []
    assert "REMINDERS_ENABLED" in out


async def test_no_database_says_so_instead_of_promising() -> None:
    """Null-магазин = «не могу»: «поставил» без хранилища — это потерянная договорённость."""
    with override_settings(timezone="Europe/Moscow", reminders_enabled=True):
        out = await tools.set_reminder(
            tools.SetReminderArgs(text="хлеб", when="через час"), ctx(NullReminderStore())
        )
    assert "не куда" in out or "недоступна" in out
    assert "Поставлено" not in out


async def test_store_error_is_reported_as_failure_not_as_success() -> None:
    store = Store(fail_add=RuntimeError("deadlock detected"))
    with override_settings(timezone="Europe/Moscow", reminders_enabled=True):
        out = await tools.set_reminder(
            tools.SetReminderArgs(text="хлеб", when="через час"), ctx(store)
        )
    assert out.startswith("Не сохранилось") and "deadlock" in out


async def test_list_reminders_shows_labels_and_ids() -> None:
    store = Store(
        scheduled=[reminder(), reminder(id="a" * 8 + "-x", text="забрать химчистку")],
    )
    with override_settings(timezone="Europe/Moscow"):
        out = await tools.list_reminders(tools.ListArgs(limit=10), ctx(store))
    assert "позвонить в банк" in out and "забрать химчистку" in out
    assert out.count("\n") == 1


async def test_list_is_honest_when_empty() -> None:
    with override_settings(timezone="Europe/Moscow"):
        out = await tools.list_reminders(tools.ListArgs(limit=10), ctx(Store(scheduled=[])))
    assert out == "Напоминаний не запланировано."


async def test_cancel_by_id_or_by_word() -> None:
    item = reminder()
    store = Store(scheduled=[item])
    with override_settings(timezone="Europe/Moscow"):
        by_id = await tools.cancel_reminder(tools.CancelArgs(ref=item.short_id), ctx(store))
        nothing = await tools.cancel_reminder(tools.CancelArgs(ref="ничего похожего"), ctx(store))
    assert "Отменено" in by_id
    assert "не найдено" in nothing


def test_the_application_loads_reminder_tools_once() -> None:
    """Одна функция на сборку реестра: `aegis tools`, `aegis ask` и бот видят одинаковый набор.

    Второй вызов — проверка идемпотентности: `registry.register` на повторе бросает «уже
    зарегистрирован», а значит импорт ради сайд-эффекта в двух местах сделал бы сборку падёжной.
    """
    from aegis.agents.tools import load_builtin_tools
    from aegis.agents.tools.registry import registry as app_registry

    load_builtin_tools()
    load_builtin_tools()
    names = set(app_registry.names(include_disabled=True))
    assert {"set_reminder", "list_reminders", "cancel_reminder"} <= names
    assert app_registry.get("set_reminder").writes, "запись в расписание — это запись"
    assert not app_registry.get("list_reminders").writes


# ------------------------------------------------------------------ тик --


async def test_tick_sends_and_marks_each_reminder_once() -> None:
    due = [
        reminder(),
        reminder(id="b" * 8 + "-y", text="выгул", due_at=datetime(2026, 9, 5, 11, 0, tzinfo=UTC)),
    ]
    store = Store(due=due)
    sent: list[str] = []

    async def send(item: Reminder) -> None:
        sent.append(item.text)

    report = await deliver(store, send=send)
    assert sent == ["позвонить в банк", "выгул"]
    assert report.claimed == 2 and len(report.sent) == 2
    assert store.sent == [due[0].id, due[1].id]
    assert "отправлено 2 из 2" in report.summary()


async def test_failed_delivery_keeps_the_reminder_for_the_next_tick() -> None:
    store = Store(due=[reminder(attempts=1)])
    boom = RuntimeError("telegram: 429 too many requests")

    async def send(item: Reminder) -> None:
        raise boom

    report = await deliver(store, send=send)
    assert report.sent == [] and report.failed == [reminder().short_id]
    assert store.sent == [], "не доставленное не помечается доставленным"
    assert report.exhausted == [], "пока есть попытки — это сбой, а не приговор"
    sent_id, error = store.errors[0]
    assert "429" in error and sent_id == store.due[0].id


async def test_last_attempt_is_reported_as_exhausted() -> None:
    store = Store(due=[reminder(attempts=MAX_ATTEMPTS)])

    async def send(item: Reminder) -> None:
        raise RuntimeError("нет сети")

    report = await deliver(store, send=send)
    assert report.exhausted == [reminder().short_id]
    assert "нужна реакция" in report.summary()


async def test_empty_tick_is_not_an_error() -> None:
    report = await deliver(Store(due=[]), send=lambda item: pytest.fail("не должно вызываться"))  # type: ignore[arg-type]
    assert report.claimed == 0
    assert report.summary() == "напоминаний по времени сейчас нет"


def test_report_defaults_are_sane() -> None:
    assert DeliverReport().summary() == "напоминаний по времени сейчас нет"


def test_deliver_prefix_is_part_of_the_message() -> None:
    assert DELIVER_PREFIX.strip() == "⏰ Напоминание:"


# ---------------------------------------------------------------- магазин --


async def test_sql_store_refuses_naive_time() -> None:
    """Наивный момент уехал бы в пояс сервера: в 9:00 по Москве пришло бы в 6:00 или в 12:00.

    Проверка до обращения к БД — то есть офлайн и без двойников: это инвариант, а не поведение SQL.
    """
    store = SqlReminderStore()
    with pytest.raises(ValueError, match="tz-aware"):
        await store.add(owner_id=1, body="завтра", due_at=datetime(2026, 9, 6, 9, 0))


def test_null_store_is_marked_disabled() -> None:
    store = NullReminderStore()
    assert store.enabled is False


async def test_null_store_refuses_to_add_and_returns_empty_lists() -> None:
    store = NullReminderStore()
    with pytest.raises(RuntimeError, match="база недоступна"):
        await store.add(owner_id=1, body="x", due_at=datetime.now(UTC))
    assert await store.list_scheduled(owner_id=1) == []
    assert await store.claim_due() == []
    assert await store.cancel(owner_id=1, ref="что-угодно") is None


async def test_status_slices_the_schedule_for_the_owner() -> None:
    """Срез для ``/status``: включено, сколько живёт, сколько просрочено — и ошибка как данные."""
    cfg = Settings(_env_file=None, _env_prefix="T_", glm_api_key="k")  # type: ignore[call-arg]
    store = Store()
    store.counts = lambda: _counts({"scheduled": 3, "overdue": 1, "sent": 40, "failed": 0})  # type: ignore[method-assign]
    info = await _reminders_status(store, cfg)
    assert info["enabled"] is True and info["scheduled"] == 3 and info["overdue"] == 1

    broken = Store()

    async def boom() -> dict[str, int]:
        raise RuntimeError("база лежит")

    broken.counts = boom  # type: ignore[method-assign]
    info = await _reminders_status(broken, cfg)
    assert "база лежит" in info["error"], "сбой счётчика — это факт для владельца, а не исключение"
    assert info["enabled"] is True


def _counts(value: dict[str, int]) -> Any:
    async def ready() -> dict[str, int]:
        return value

    return ready()


def test_reminder_label_can_be_shown_in_owner_timezone() -> None:
    """Список и сухой прогон обязаны называть один и тот же час: id тот же, время то же."""
    item = reminder()  # 05.09 12:00 UTC
    assert "12:00 UTC" in item.label()
    assert "15:00 Europe/Moscow" in item.label("Europe/Moscow")


def test_plural_endings_are_russian_not_latin() -> None:
    from aegis.cli import _plural

    assert _plural(1, "напоминание", "напоминания", "напоминаний") == "1 напоминание"
    assert _plural(2, "напоминание", "напоминания", "напоминаний") == "2 напоминания"
    assert _plural(5, "напоминание", "напоминания", "напоминаний") == "5 напоминаний"
    assert _plural(11, "напоминание", "напоминания", "напоминаний") == "11 напоминаний"
    assert _plural(21, "напоминание", "напоминания", "напоминаний") == "21 напоминание"


def test_reminder_label_mentions_id_time_and_error() -> None:
    item = reminder(attempts=2, last_error="telegram 429")
    label = item.label()
    assert label.startswith(item.short_id) and "05.09 12:00" in label
    assert "попытка 2" in label and "429" in label


async def test_store_reports_how_many_attempts_are_left() -> None:
    """`attempts` приходит из БД: без него «failed» выглядел бы просто молчанием."""
    store = Store(due=[reminder(attempts=MAX_ATTEMPTS - 1)])
    calls = 0

    async def send(item: Reminder) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("сбой")

    report = await deliver(store, send=send)
    assert calls == 1 and report.exhausted == []
