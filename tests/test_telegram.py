"""Telegram-слой: whitelist, inline-подтверждения, безопасная отправка.

Хендлеры проверяются без сети: aiogram-объекты подменяются минимальными двойниками, а
`OwnerOnly` и `send_reply` — чистая логика, и она обязана быть надёжной (это граница приватности).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest

from aegis.agents.supervisor import Inbound, PendingAction, Reply
from aegis.interaction.telegram.bot import (
    OwnerOnly,
    UpdateDedup,
    _confirm_keyboard,
    _drain_queue,
    _inbound,
    _reply_markup,
    _stream_label,
    on_appeal,
    run,
    send_reply,
)


async def test_owner_only_lets_the_owner_through() -> None:
    seen: list[str] = []

    async def handler(event: Any, data: dict[str, Any]) -> str:
        seen.append("called")
        return "ok"

    middleware = OwnerOnly(42)
    result = await middleware(
        handler, SimpleNamespace(), {"event_from_user": SimpleNamespace(id=42)}
    )
    assert result == "ok"
    assert seen == ["called"]


async def test_owner_only_silently_ignores_everyone_else() -> None:
    async def handler(event: Any, data: dict[str, Any]) -> str:
        raise AssertionError("хендлер не должен быть вызван")

    middleware = OwnerOnly(42)
    for data in ({"event_from_user": SimpleNamespace(id=1)}, {"event_from_user": None}, {}):
        assert await middleware(handler, SimpleNamespace(), data) is None


async def test_owner_only_compares_ids_as_ints() -> None:
    async def handler(event: Any, data: dict[str, Any]) -> str:
        return "ok"

    # Telegram отдаёт int; строка «42» не должна открывать доступ
    assert (
        await OwnerOnly(42)(
            handler, SimpleNamespace(), {"event_from_user": SimpleNamespace(id="42x")}
        )
        is None
    )


class RecordingBot:
    def __init__(self, fail_html_once: bool = False) -> None:
        self.sent: list[tuple[int, str, Any]] = []
        self.fail_html_once = fail_html_once

    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: Any = None, link_preview_options: Any = None
    ) -> SimpleNamespace:
        if self.fail_html_once and "<b>" in text:
            self.fail_html_once = False
            raise TelegramBadRequest(method="sendMessage", message="Can't parse entities")
        self.sent.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=len(self.sent))


async def test_send_reply_splits_long_messages() -> None:
    bot = RecordingBot()
    reply = Reply(text="x" * 9000)
    await send_reply(bot, 7, reply)  # type: ignore[arg-type]
    assert len(bot.sent) == 3
    assert all(len(text) <= 4096 for _, text, _ in bot.sent)
    assert "".join(text for _, text, _ in bot.sent) == "x" * 9000


async def test_send_reply_falls_back_to_readable_text() -> None:
    """Telegram не принял разметку -> отправляем ТЕКСТ, а не экранированный HTML."""
    bot = RecordingBot(fail_html_once=True)
    await send_reply(bot, 7, Reply(text="<b>важно</b>"))  # type: ignore[arg-type]
    assert bot.sent[0][1] == "важно"


async def test_send_reply_fallback_keeps_line_breaks() -> None:
    """Запасной путь обязан сохранить читаемость: переносы вместо <br>, текст вместо тегов."""
    bot = RecordingBot(fail_html_once=True)
    await send_reply(
        bot, 7, Reply(text="Модели недоступны.<br><b>401</b><br><code>/status</code> покажет")
    )  # type: ignore[arg-type]
    assert bot.sent[0][1] == "Модели недоступны.\n401\n/status покажет"


async def test_confirmation_buttons_attached_to_last_chunk() -> None:
    bot = RecordingBot()
    pending = [
        PendingAction(
            tool="remember_fact", args={"fact": "не пить кофе"}, call_id="c1", reason="тест"
        )
    ]
    reply = Reply(text="нужно подтверждение", pending=pending, pending_id="abc123")
    await send_reply(bot, 7, reply)  # type: ignore[arg-type]
    markup = bot.sent[-1][2]
    assert markup is not None
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert any("Выполнить" in label for label in labels)
    callback_data = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert "ok:abc123" in callback_data and "no:abc123" in callback_data


def test_confirm_keyboard_label_counts_actions() -> None:
    single = _confirm_keyboard("pid1", 1)
    assert single.inline_keyboard[0][0].text.endswith("Выполнить")
    many = _confirm_keyboard("pid2", 3)
    assert "3" in many.inline_keyboard[0][0].text


async def test_send_reply_raises_only_on_unrecoverable_telegram_error() -> None:
    class BrokenBot:
        async def send_message(self, *args: Any, **kwargs: Any) -> Any:
            raise TelegramBadRequest(method="sendMessage", message="chat not found")

    with pytest.raises(TelegramBadRequest):
        await send_reply(BrokenBot(), 7, Reply(text="текст"))  # type: ignore[arg-type]


def test_trace_label_needs_more_than_an_open_port() -> None:
    """«Трассировка: ok» — только если строки реально пишутся.

    Инцидент «Сбой: TypeError» выглядел как «БД ок, а бот ломается»: connect-ok ≠ schema-ok,
    и подпись в /status обязана это различать.
    """
    from aegis.interaction.telegram.bot import _trace_label

    ok = {"tracing_degraded": False, "tracing_failures": 0}
    broken = {"tracing_degraded": True, "tracing_failures": 2}
    ready = SimpleNamespace(db_ready=True)
    assert _trace_label(ready, ok) == "события/аудит в БД"
    assert "alembic upgrade head" in _trace_label(ready, broken)
    assert "2 сбоя" in _trace_label(ready, broken)
    assert _trace_label(SimpleNamespace(db_ready=False), ok) == "БД не настроена — только память"


def test_status_keeps_journal_and_polish_apart() -> None:
    """Три обещания — три строки: журнал пишется, ответ сверяется, чужое размечается.

    Все три могут быть включены по-разному, и «всё хорошо» из одной строки не следует из другой:
    ровно так же, как «аудит пишется» ≠ «ход воспроизводим».
    """
    from aegis.interaction.telegram.bot import _polish_line, _repro_line

    full = {
        "repro_enabled": True,
        "repro_failures": 0,
        "answer_polish": {"verify": True, "quarantine": True, "always": True},
    }
    off = {
        "repro_enabled": True,
        "repro_failures": 0,
        "answer_polish": {"verify": False, "quarantine": False, "always": False},
    }
    assert "ведётся" in _repro_line(full)
    assert "сверка с источниками вкл" in _polish_line(full)
    assert "карантин внешнего текста вкл" in _polish_line(full)
    assert "не проверяются" in _polish_line(off)
    assert "карантин внешнего текста выкл" in _polish_line(off)
    assert _polish_line({}) != _repro_line({})


def test_status_reminders_line_names_the_reason_for_silence() -> None:
    """«Напоминание не пришло» — это три разных диагноза: выключено, нет БД, тик не догнал."""
    from aegis.interaction.telegram.bot import _reminders_line

    assert "некуда сохранять" in _reminders_line({})
    assert "некуда сохранять" in _reminders_line({"reminders": {"enabled": False, "batch": 20}})
    quiet = _reminders_line(
        {"reminders": {"enabled": True, "scheduled": 2, "overdue": 0, "batch": 20}}
    )
    assert "2 в расписании" in quiet and "⚠️" not in quiet
    late = _reminders_line(
        {"reminders": {"enabled": True, "scheduled": 2, "overdue": 1, "failed": 1, "batch": 5}}
    )
    assert "1 пора" in late and "исчерпанными" in late and "тик ≤ 5" in late
    broken = _reminders_line({"reminders": {"enabled": True, "error": "connection refused"}})
    assert "счётчик не читается" in broken and "connection refused" in broken


def test_status_notes_line_distinguishes_lag_from_absence() -> None:
    """«Поиск находит не то» — это очередь индексации или её отсутствие: разные ответы владельцу."""
    from aegis.interaction.telegram.bot import _notes_line

    assert "счётчик индекса недоступен" in _notes_line({})
    assert "счётчик индекса недоступен" in _notes_line({"notes": {"available": False}})
    clean = _notes_line({"notes": {"available": True, "pending": 0}})
    assert "построены для всех" in clean and "⚠️" not in clean
    late = _notes_line({"notes": {"available": True, "pending": 162}})
    assert "162 ждут эмбеддинга" in late and "aegis index notes" in late


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/restart", "restart"),
        ("/ReStart", "restart"),
        ("/nope@RockBot arg", "nope"),
        ("/start", None),
        ("/status", None),
        ("/halt причина", None),
        ("расскажи про себя", None),
        ("/home/user/Rock/README.md", None),
        ("", None),
    ],
)
def test_unknown_command_detection(text: str, expected: str | None) -> None:
    from aegis.interaction.telegram.bot import _unknown_command

    assert _unknown_command(text) == expected


async def test_unknown_command_answered_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    """«/restart» не должен уходить в модель: ответ дешёвый и честный."""
    from aegis.interaction.telegram import bot as bot_mod

    sent: list[str] = []
    handled: list[str] = []

    async def answer(text: str, *args: Any, **kwargs: Any) -> None:
        sent.append(text)

    async def fake_run(message: Any, app: Any, inbound: Any, bot: Any) -> None:
        handled.append(inbound.text)

    monkeypatch.setattr(bot_mod, "run", fake_run)
    message = SimpleNamespace(from_user=SimpleNamespace(id=1), text="/restart", answer=answer)
    await bot_mod.on_text(message, SimpleNamespace(), None)  # type: ignore[arg-type]

    assert not handled, "неизвестная команда не отправляется в LLM"
    assert "/restart" in sent[0]
    assert "docker compose" in sent[0], "про перезапуск контейнера надо сказать, где его делают"

    await bot_mod.on_text(
        SimpleNamespace(from_user=SimpleNamespace(id=1), text="привет", answer=answer),
        SimpleNamespace(),
        None,  # type: ignore[arg-type]
    )
    assert handled == ["привет"]


# ------------------------------------------------------------------ живой ответ в чате


class DraftMessage:
    """Message, у которого «индикатор» и есть будущий ответ: `answer` возвращает себя.

    Так устроено в проде (`status = await message.answer("…")`, потом правки того же сообщения), и
    тест обязан видеть именно эту связь: черновик либо превращается в ответ, либо удаляется.
    """

    def __init__(self, *, edit_fails: bool = False) -> None:
        self.chat = SimpleNamespace(id=7)
        self.bot = None
        self.edits: list[dict[str, Any]] = []
        self.deleted = False
        self.edit_fails = edit_fails

    async def answer(self, text: str, *args: Any, **kwargs: Any) -> DraftMessage:
        self.asked = text
        return self

    async def edit_text(
        self,
        text: str | None = None,
        *,
        parse_mode: Any = None,
        reply_markup: Any = None,
        link_preview_options: Any = None,
        **kwargs: Any,
    ) -> None:
        if self.edit_fails:
            raise TelegramBadRequest(method="editMessageText", message="can't parse entities")
        self.edits.append({"text": text, "parse_mode": parse_mode, "reply_markup": reply_markup})

    async def delete(self) -> None:
        self.deleted = True


class DraftSupervisor:
    """Отдаёт заготовленный ответ и досылает куски ровно так, как это делает шлюз."""

    def __init__(self, reply: Reply, *, pieces: tuple[str, ...] = ("при", "вет")) -> None:
        self.reply = reply
        self.pieces = pieces
        self.sinks: list[Any] = []

    async def handle(self, inbound: Any, *, on_delta: Any = None) -> Reply:
        self.sinks.append(on_delta)
        if on_delta is not None:
            for piece in self.pieces:
                await on_delta(piece)
        return self.reply


def draft_app(supervisor: DraftSupervisor, **cfg: Any) -> Any:
    from aegis.platform.config import Settings

    base: dict[str, Any] = {"stream_replies": True}
    base.update(cfg)
    return SimpleNamespace(cfg=Settings(**base), supervisor=supervisor)  # type: ignore[arg-type]


async def test_streamed_answer_becomes_the_draft_message() -> None:
    sup = DraftSupervisor(Reply(text="привет, всё хорошо"))
    message = DraftMessage()
    bot = RecordingBot()

    await run(message, draft_app(sup), Inbound(text="как дела", owner_id=1), bot)  # type: ignore[arg-type]

    assert sup.sinks[0] is not None, "при включённом стриминге интерфейс обязан передать приёмник"
    assert message.edits[-1]["text"] == "привет, всё хорошо"
    assert message.edits[-1]["parse_mode"] == "HTML", "формат догоняется в финальной правке"
    assert not message.deleted, "черновик не удаляют: он и есть ответ"
    assert bot.sent == [], "второе сообщение с тем же текстом — это дубль"


async def test_streaming_off_keeps_the_usual_path() -> None:
    sup = DraftSupervisor(Reply(text="ответ"))
    message = DraftMessage()
    bot = RecordingBot()

    await run(
        message,
        draft_app(sup, stream_replies=False),
        Inbound(text="привет", owner_id=1),
        bot,  # type: ignore[arg-type]
    )

    assert sup.sinks[0] is None, "без стриминга шлюз не дёргают стримом"
    assert message.deleted is True
    assert bot.sent[0][1] == "ответ"


async def test_long_answer_is_not_left_in_the_draft() -> None:
    sup = DraftSupervisor(Reply(text="д" * 9000))
    message = DraftMessage()
    bot = RecordingBot()

    await run(message, draft_app(sup), Inbound(text="длинно", owner_id=1), bot)  # type: ignore[arg-type]

    assert bot.sent and message.deleted is True, "многочастичный ответ отправляется целиком"
    assert "".join(text for _, text, _ in bot.sent) == "д" * 9000


async def test_failed_edit_does_not_eat_the_answer() -> None:
    sup = DraftSupervisor(Reply(text="ответ"))
    message = DraftMessage(edit_fails=True)
    bot = RecordingBot()

    await run(message, draft_app(sup), Inbound(text="привет", owner_id=1), bot)  # type: ignore[arg-type]

    assert message.edits == []
    assert bot.sent[0][1] == "ответ", "сбой правки обязан уйти в обычную отправку, а не в тишину"


def test_status_names_the_answer_mode() -> None:
    """Режим ответа видно в /status: «молчит и правит» и «молчит и ждёт» — разные сбои."""

    off = SimpleNamespace(stream_replies=False, stream_edit_interval_ms=900)
    on = SimpleNamespace(stream_replies=True, stream_edit_interval_ms=1500)

    assert "выключен" in _stream_label(off)  # type: ignore[arg-type]
    assert _stream_label(on) == "включён, правка раз в 1500 мс"  # type: ignore[arg-type]


# ------------------------------------------------------------- F1: дедуп апдейтов


class _SeqApp:
    """App-двойник: seen_update отдаёт заранее заготовленные ответы."""

    def __init__(self, answers: list[bool]) -> None:
        self._answers = answers
        self.seen: list[tuple[int, int]] = []

    async def seen_update(self, update_id: int, chat_id: int, *, kind: str = "message") -> bool:
        self.seen.append((update_id, chat_id))
        return self._answers.pop(0)


async def test_update_dedup_swallows_repeats() -> None:
    calls: list[str] = []

    async def handler(event: Any, data: dict[str, Any]) -> str:
        calls.append("run")
        return "ok"

    app = _SeqApp([True, False])
    middleware = UpdateDedup(app)
    event = SimpleNamespace(update_id=77, message=SimpleNamespace(chat=SimpleNamespace(id=-1001)))
    assert await middleware(handler, event, {}) == "ok"
    assert await middleware(handler, event, {}) is None, (
        "повтор того же update_id не доходит до хендлера"
    )
    assert calls == ["run"]
    assert app.seen == [(77, -1001), (77, -1001)]


async def test_update_dedup_passes_events_without_update_id() -> None:
    async def handler(event: Any, data: dict[str, Any]) -> str:
        return "ok"

    app = _SeqApp([])
    assert await UpdateDedup(app)(handler, SimpleNamespace(update_id=None), {}) == "ok"
    assert app.seen == []


async def test_seen_update_memory_fallback_is_process_local_and_bounded() -> None:
    """Без БД дедуп остаётся, но только «в этом процессе»: это деградация, и она ограничена.»"""
    from aegis.runtime import App

    fake = SimpleNamespace(db_ready=False, _dedup_mem={})
    assert await App.seen_update(fake, 10, 1) is True
    assert await App.seen_update(fake, 10, 1) is False
    # заполняем до потолка — старые ids вытесняются, место не растёт бесконечно
    fake._dedup_mem = {i: None for i in range(10_000)}
    assert await App.seen_update(fake, 99_999, 1) is True
    assert len(fake._dedup_mem) == 10_000 and 0 not in fake._dedup_mem


# ------------------------------------------------------------- F2: roster в гейте


async def test_owner_only_admits_roster_and_no_one_else() -> None:
    async def handler(event: Any, data: dict[str, Any]) -> str:
        return "ok"

    gate = OwnerOnly(42, frozenset({7}))
    assert (
        await gate(handler, SimpleNamespace(), {"event_from_user": SimpleNamespace(id=42)}) == "ok"
    )
    assert (
        await gate(handler, SimpleNamespace(), {"event_from_user": SimpleNamespace(id=7)}) == "ok"
    )
    assert (
        await gate(handler, SimpleNamespace(), {"event_from_user": SimpleNamespace(id=9)}) is None
    )
    assert (
        await gate(handler, SimpleNamespace(), {"event_from_user": SimpleNamespace(id="7x")})
        is None
    )


def test_inbound_separates_household_from_actor() -> None:
    app = SimpleNamespace(cfg=SimpleNamespace(telegram_owner_id=1))
    message = SimpleNamespace(from_user=SimpleNamespace(id=7))
    inbound = _inbound(app, message, text="привет")
    assert inbound.owner_id == 1 and inbound.actor_id == 7
    # приложения без конфига (юнит-двойники) не должны падать: household = сам спрашивающий
    loose = _inbound(SimpleNamespace(), message, text="привет")
    assert loose.owner_id == 7 and loose.actor_id == 7


# ------------------------------------------------------------- F5: апелляция в чате


def test_reply_markup_prefers_confirmation_over_appeal() -> None:
    appeal = Reply(text="нельзя", appeal_id="a1", appealable=True)
    markup = _reply_markup(appeal)
    assert markup is not None
    button = markup.inline_keyboard[0][0]
    assert button.callback_data == "ap:a1"

    with_pending = Reply(
        text="подтверди",
        pending=[PendingAction(tool="pay", args={}, reason="r", rule="x", call_id="c1")],
        pending_id="p1",
        appealable=True,
        appeal_id="a2",
    )
    data = _reply_markup(with_pending).inline_keyboard[0]
    assert data[0].callback_data == "ok:p1", (
        "пока есть прямое подтверждение, апелляция не дублирует выбор"
    )


async def test_on_appeal_delivers_to_owner_and_extinguishes_button() -> None:
    sent: list[tuple[int, str, Any]] = []
    edits: list[Any] = []

    class Msg:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(id=-1)

        async def edit_reply_markup(self, *, reply_markup: Any = None) -> None:
            edits.append(reply_markup)

    async def snapshot(appeal_id: str) -> dict[str, Any] | None:
        return {
            "appeal": True,
            "owner_id": 42,
            "actor_id": 7,
            "actions": [
                {"tool": "finance_pay", "reason": "нет права tool:pay", "rule": "grant-missing@1"}
            ],
        }

    class Bot:
        async def send_message(
            self, chat_id: int, text: str, reply_markup: Any = None, **kw: Any
        ) -> None:
            sent.append((chat_id, text, reply_markup))

    class Callback:
        def __init__(self) -> None:
            self.data = "ap:a1"
            self.from_user = SimpleNamespace(id=7)
            self.message = Msg()
            self.answered: str | None = None

        async def answer(self, text: str | None = None, **kw: Any) -> None:
            self.answered = text

    app = SimpleNamespace(
        cfg=SimpleNamespace(telegram_owner_id=42),
        supervisor=SimpleNamespace(appeal_snapshot=snapshot),
    )
    cb = Callback()
    await on_appeal(cb, app, Bot())  # type: ignore[arg-type]

    assert len(sent) == 1
    chat_id, text, markup = sent[0]
    assert chat_id == 42 and "finance_pay" in text
    row = markup.inline_keyboard[0]
    assert row[0].callback_data == "ok:a1" and row[1].callback_data == "no:a1"
    assert edits == [None], "кнопка «обжаловать» гасится после подачи"
    assert cb.answered == "Отправлено владельцу"


async def test_on_appeal_expired_snapshot_answers_honestly() -> None:
    class Bot:
        async def send_message(self, *a: Any, **k: Any) -> None:
            raise AssertionError("истёкшую апелляцию нельзя доставлять")

    async def snapshot(appeal_id: str) -> None:
        return None

    class Callback:
        data = "ap:gone"
        from_user = SimpleNamespace(id=7)
        message = None

        def __init__(self) -> None:
            self.answered: str | None = None

        async def answer(self, text: str | None = None, **kw: Any) -> None:
            self.answered = text

    cb = Callback()
    app = SimpleNamespace(
        cfg=SimpleNamespace(telegram_owner_id=42),
        supervisor=SimpleNamespace(appeal_snapshot=snapshot),
    )
    await on_appeal(cb, app, Bot())  # type: ignore[arg-type]
    assert "устарела" in (cb.answered or "")


# ------------------------------------------------------------- F1: очередь ходов


async def test_drain_queue_processes_backlog_in_order_and_stops_when_empty() -> None:
    class QueueSup:
        def __init__(self) -> None:
            self.pending = [Inbound(text="второе", owner_id=1), Inbound(text="третье", owner_id=1)]
            self.seen: list[str] = []

        async def drain_next(self, owner_id: int) -> Inbound | None:
            return self.pending.pop(0) if self.pending else None

        async def handle(self, inbound: Inbound, **kw: Any) -> Reply:
            self.seen.append(inbound.text)
            return Reply(text=f"ответ: {inbound.text}")

    class Bot:
        def __init__(self) -> None:
            self.sent: list[tuple[int, str]] = []

        async def send_message(self, chat_id: int, text: str, **kw: Any) -> None:
            self.sent.append((chat_id, text))

    sup = QueueSup()
    bot = Bot()
    app = SimpleNamespace(cfg=SimpleNamespace(turn_drain_limit=5), supervisor=sup)
    await _drain_queue(app, bot, -1, 1)  # type: ignore[arg-type]
    assert sup.seen == ["второе", "третье"]
    assert [t for _, t in bot.sent] == ["ответ: второе", "ответ: третье"]


async def test_drain_queue_limit_is_a_hard_cap() -> None:
    class Never:
        async def drain_next(self, owner_id: int) -> Inbound | None:
            return Inbound(text="ещё", owner_id=1)

    class Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, chat_id: int, text: str, **kw: Any) -> None:
            self.sent.append(text)

    async def handle(inbound: Inbound, **kw: Any) -> Reply:
        return Reply(text="ок")

    sup = SimpleNamespace(drain_next=Never().drain_next, handle=handle)
    bot = Bot()
    app = SimpleNamespace(cfg=SimpleNamespace(turn_drain_limit=3), supervisor=sup)
    await _drain_queue(app, bot, -1, 1)  # type: ignore[arg-type]
    assert len(bot.sent) == 3, "бесконечная очередь не имеет права удерживать чат вечно"


async def test_drain_queue_failure_is_reported_not_raised() -> None:
    class Boom:
        async def drain_next(self, owner_id: int) -> Inbound:
            return Inbound(text="x", owner_id=1)

        async def handle(self, inbound: Inbound, **kw: Any) -> Reply:
            raise RuntimeError("модель легла")

    class Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, chat_id: int, text: str, **kw: Any) -> None:
            self.sent.append(text)

    app = SimpleNamespace(
        cfg=SimpleNamespace(turn_drain_limit=2),
        supervisor=Boom(),
    )
    bot = Bot()
    await _drain_queue(app, bot, -1, 1)  # type: ignore[arg-type]
    assert any("не удался" in t for t in bot.sent), "сбой очереди виден в чате, а не только в логе"
