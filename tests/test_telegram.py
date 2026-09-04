"""Telegram-слой: whitelist, inline-подтверждения, безопасная отправка.

Хендлеры проверяются без сети: aiogram-объекты подменяются минимальными двойниками, а
`OwnerOnly` и `send_reply` — чистая логика, и она обязана быть надёжной (это граница приватности).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest

from aegis.agents.supervisor import PendingAction, Reply
from aegis.interaction.telegram.bot import OwnerOnly, _confirm_keyboard, send_reply


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


async def test_send_reply_falls_back_to_escaped_text() -> None:
    bot = RecordingBot(fail_html_once=True)
    await send_reply(bot, 7, Reply(text="<b>важно</b>"))  # type: ignore[arg-type]
    assert bot.sent[0][1] == "&lt;b&gt;важно&lt;/b&gt;"


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
