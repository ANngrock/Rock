"""Живой ответ в Telegram: троттлинг правок, отказ разметки, «не потеряли ответ».

`Message` подменён заглушкой: нам важно не что делает API, а что мы ему отправляем и что делаем,
когда он отказывает. Потеря ответа здесь — главный риск, поэтому большая часть теста про отказы.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from aegis.interaction.telegram import stream as stream_mod
from aegis.interaction.telegram.stream import (
    MIN_INTERVAL_S,
    PLACEHOLDER,
    TelegramStream,
    make_stream,
)

INTERVAL = MIN_INTERVAL_S


class FakeMessage:
    """`edit_text` со списком ответов: исключение на N-м вызове, дальше — тишина."""

    def __init__(self, *, raises: dict[int, BaseException] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raises = dict(raises or {})

    async def edit_text(
        self,
        text: str | None = None,
        *,
        parse_mode: Any = None,
        reply_markup: Any = None,
        link_preview_options: Any = None,
        **kwargs: Any,
    ) -> None:
        self.calls.append(
            {
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
                "link_preview_options": link_preview_options,
            }
        )
        failure = self._raises.get(len(self.calls))
        if failure is not None:
            raise failure

    @property
    def texts(self) -> list[str | None]:
        return [call["text"] for call in self.calls]


def stream_of(message: FakeMessage, **kwargs: Any) -> TelegramStream:
    kwargs.setdefault("interval_s", INTERVAL)
    return TelegramStream(message, **kwargs)  # type: ignore[arg-type]


async def _wait() -> None:
    """Переждать паузу правок — время здесь измеряется им, а не числом из теста."""

    await asyncio.sleep(INTERVAL * 1.2)


# ------------------------------------------------------------------ троттлинг


async def test_deltas_within_the_pause_do_not_edit_the_message() -> None:
    msg = FakeMessage()
    stream = stream_of(msg)
    await stream.push("При")
    await stream.push("вет")

    assert len(msg.calls) == 1, "правка на каждый токен — это лимит сообщений чата, а не скорость"
    assert msg.texts[0] == "При"


async def test_next_edit_happens_after_the_pause() -> None:
    msg = FakeMessage()
    stream = stream_of(msg)
    await stream.push("При")
    await _wait()
    await stream.push("вет")

    assert msg.texts == ["При", "Привет"], "правки идут накопленным текстом, а не кусками"


async def test_identical_text_is_not_sent_again() -> None:
    msg = FakeMessage()
    stream = stream_of(msg)
    await stream.push("ок")
    await _wait()
    await stream.push("")
    await _wait()

    assert len(msg.calls) == 1, (
        "Telegram отвечает ошибкой на «не изменилось» — не надо в это попадать"
    )


async def test_live_text_is_plain_and_trimmed_to_the_limit() -> None:
    msg = FakeMessage()
    stream = stream_of(msg, limit=200)
    await _wait()
    await stream.push("а" * 400)

    text = msg.texts[0] or ""
    assert len(text) == 200 and text.endswith("…"), "черновик умещается в сообщение целиком"
    assert msg.calls[0]["parse_mode"] is None, "незакрытая разметка в полуответе = 400 на правке"


# ------------------------------------------------------------------ финал


async def test_finish_replaces_the_placeholder_with_formatted_answer() -> None:
    msg = FakeMessage()
    stream = stream_of(msg)
    ok = await stream.finish("<b>готово</b>")

    assert ok is True
    assert msg.texts == ["<b>готово</b>"]
    assert msg.calls[0]["parse_mode"] == "HTML"


async def test_finish_repairs_broken_markup_before_editing() -> None:
    msg = FakeMessage()
    stream = stream_of(msg)
    assert await stream.finish("<b>готово")

    assert msg.texts == ["<b>готово</b>"], "разметку чиним (закрываем тег), а не выбрасываем"


async def test_markup_of_the_reply_is_carried_to_the_final_edit() -> None:
    msg = FakeMessage()
    stream = stream_of(msg)
    markup = SimpleNamespace(inline_keyboard=[["ok"]])
    assert await stream.finish("нужно подтверждение", markup=markup)  # type: ignore[arg-type]

    assert msg.calls[0]["reply_markup"] is markup, "кнопка обязана остаться на том же сообщении"


async def test_multi_message_answer_falls_back_to_the_ordinary_path() -> None:
    msg = FakeMessage()
    stream = stream_of(msg)
    ok = await stream.finish("текст" * 2000)

    assert ok is False, "длинный ответ обязан идти через send_reply: черновик его не донесёт"
    assert msg.calls == []


async def test_nothing_happens_after_finish() -> None:
    msg = FakeMessage()
    stream = stream_of(msg)
    await stream.finish("итог")
    await _wait()
    await stream.push("хвост")

    assert len(msg.calls) == 1, "после финала правок быть не может: они затрут готовый ответ"


# ------------------------------------------------------------------ отказы


async def test_broken_edit_disables_the_stream_and_answer_goes_away_intact() -> None:
    msg = FakeMessage(raises={1: RuntimeError("network down")})
    stream = stream_of(msg)
    await _wait()
    await stream.push("а")
    await _wait()
    await stream.push("б")
    ok = await stream.finish("аб")

    assert len(msg.calls) == 1, "сломанный стрим не должен долбить API весь ход"
    assert stream.broken is True
    assert ok is False, "False = вызывающий код отправит ответ обычно; текст не теряется"


async def test_not_modified_is_not_a_failure() -> None:
    err = TelegramBadRequest(method=SimpleNamespace(), message="message is not modified")
    msg = FakeMessage(raises={1: err})
    stream = stream_of(msg)
    await _wait()
    await stream.push("ок")

    assert stream.broken is False
    assert await stream.finish("ок") is True


async def test_rejected_markup_disables_stream_but_final_answer_is_resendable() -> None:
    err = TelegramBadRequest(method=SimpleNamespace(), message="can't parse entities")
    msg = FakeMessage(raises={1: err})
    stream = stream_of(msg)
    await _wait()
    await stream.push("а")

    assert stream.broken is True
    assert await stream.finish("а") is False


async def test_retry_after_waits_and_keeps_the_stream_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    # подменяем имя в модуле, а не `asyncio.sleep` глобально: сам тест тоже спит, и его
    # ожидание попало бы в счётчик
    monkeypatch.setattr(stream_mod, "asyncio", SimpleNamespace(sleep=fake_sleep))
    err = TelegramRetryAfter(method=SimpleNamespace(), message="flood", retry_after=40)
    msg = FakeMessage(raises={1: err})
    stream = stream_of(msg)
    await _wait()
    await stream.push("а")

    assert slept == [2.0], "ждём ровно столько, сколько можно ждать посреди хода"
    assert stream.broken is False, "429 — это «позже», а не «стрим сломан»"


# ------------------------------------------------------------------ включение


def test_make_stream_is_off_by_default_and_follows_the_settings() -> None:
    msg = FakeMessage()
    off = SimpleNamespace(stream_replies=False, stream_edit_interval_ms=900)
    on = SimpleNamespace(stream_replies=True, stream_edit_interval_ms=1500)

    assert make_stream(msg, off) is None  # type: ignore[arg-type]
    assert make_stream(None, on) is None
    live = make_stream(msg, on)  # type: ignore[arg-type]
    assert live is not None and live._interval == 1.5


def test_placeholder_of_the_draft_is_the_same_string_the_bot_sends() -> None:
    """Черновик и `PLACEHOLDER` — одна строка: иначе первая правка «не изменилось» неизбежна."""

    assert PLACEHOLDER == "…"
