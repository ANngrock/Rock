"""Живой ответ в Telegram: одно сообщение, которое правится по таймеру.

Почему правка, а не «сообщение на каждый кусок»: сорок кусков — это сорок уведомлений и разорванный
диалог в истории, который потом нельзя прочитать. Почему не правка на каждый токен: лимит Telegram
(~20 сообщений в минуту в чат) считается суммарно на отправку и на редактирование, и он делится со
всяким остальным, что бот пишет в этот чат. Отсюда пауза по умолчанию в 900 мс.

Текст по ходу дела уходит БЕЗ `parse_mode`. Причина не в лени: разметка требует парных тегов, а
«<b>Приве» — это 400, то есть каждое второе редактирование падало бы на полуслове. Формат догоняется
в :meth:`TelegramStream.finish`, где текст уже полный.

Ответ не теряется ни при каком исходе — это главное свойство класса:

* любое падение правки помечает стрим сломанным, дальше только молча копит текст;
* `finish` отвечает «не обрабатываю», если живое сообщение недоступно ИЛИ если ответ не влезает в
  одно сообщение — вызывающий код удаляет черновик и отправляет обычно, через `send_reply`.

Второе важнее, чем кажется: многосоставный ответ — не черновик, его читают целиком, и путь для него
уже есть, протестирован и умеет разметку, кнопки и ошибки.
"""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any

import structlog
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message

from aegis.interaction.telegram.render import render_for_telegram

log = structlog.get_logger(__name__)

#: то, чем бот занимает место до первого куска текста; сверяемся с ним, чтобы не слать «не
#: изменилось» (для Telegram это отдельная ошибка, а не тишина)
PLACEHOLDER = "…"
#: жёсткий пол паузы между правками: 200 мс = пять правок в секунду, дальше начинается 429
MIN_INTERVAL_S = 0.2

__all__ = ["MIN_INTERVAL_S", "PLACEHOLDER", "TelegramStream"]


class TelegramStream:
    """Черновик ответа в одном сообщении.

    Создаётся на уже отправленный placeholder — иначе «где рисовать» пришлось бы решать здесь, и
    тогда у нас появилось бы два места, знающих про сообщение хода.
    """

    def __init__(self, message: Message, *, interval_s: float = 0.9, limit: int = 4096) -> None:
        self._message = message
        self._interval = max(float(interval_s), MIN_INTERVAL_S)
        self._limit = max(int(limit), 200)
        self._buf: list[str] = []
        self._sent: str | None = None
        # «пауза» отсчитывается не от создания стрима: черновик («…») стоит на экране с начала хода,
        # поэтому первая правка законна сразу — иначе самый нетерпеливый кусок ждал бы свою паузу
        self._at = monotonic() - self._interval
        self._broken = False
        self._done = False

    @property
    def broken(self) -> bool:
        return self._broken

    @property
    def collected(self) -> str:
        return "".join(self._buf)

    async def push(self, delta: str) -> None:
        """Кусок текста от шлюза. Сinks-совместимая сигнатура, поэтому supervisor знает только её.

        Правим не чаще паузы и только когда есть чем: иначе получается гонка «edit → not modified»,
        которая выглядит как успех и при этом жрёт лимит чата.
        """
        if self._broken or self._done or not delta:
            return
        self._buf.append(delta)
        now = monotonic()
        if now - self._at < self._interval:
            return
        self._at = now
        text = self._preview()
        if text == self._sent:
            return
        if await self._edit(text):
            self._sent = text

    async def finish(
        self,
        text: str,
        *,
        markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        """Отдать итог. `True` — сообщение на месте, обычная отправка не нужна.

        Разметку применяем здесь и только здесь: полный ответ уже собрался, `sanitize_html` даёт
        валидный HTML, и правка либо проходит, либо возвращает False — полутонов нет.
        """
        self._done = True
        if self._broken:
            return False
        chunks = render_for_telegram(text).chunks or [text]
        if len(chunks) > 1:
            log.debug("telegram.stream_too_long", chunks=len(chunks))
            return False
        return await self._edit(chunks[0], html=True, markup=markup)

    # ------------------------------------------------------------------ внутреннее

    def _preview(self) -> str:
        text = self.collected.strip()
        if len(text) > self._limit:
            #: хвост не обещаем: он придёт финальной правкой, если влезет в одно сообщение
            text = text[: self._limit - 1].rstrip() + "…"
        return text or PLACEHOLDER

    async def _edit(
        self, text: str, *, html: bool = False, markup: InlineKeyboardMarkup | None = None
    ) -> bool:
        try:
            await self._message.edit_text(
                text,
                parse_mode=ParseMode.HTML if html else None,
                reply_markup=markup,
                link_preview_options=None,
            )
        except TelegramRetryAfter as exc:
            # лимит чата — это не «стрим сломан»: подождать и править дальше. Долго ждать нельзя:
            # мы держим ход, а не бесконечный таймер, поэтому берём максимум две секунды
            await asyncio.sleep(min(float(exc.retry_after or 1), 2.0))
            return False
        except TelegramBadRequest as exc:
            if _is_noop(exc):
                return True
            log.debug("telegram.stream_edit_rejected", err=repr(exc)[:200])
            self._broken = True
            return False
        except TelegramAPIError as exc:
            log.warning("telegram.stream_edit_failed", err=repr(exc)[:200])
            self._broken = True
            return False
        except Exception as exc:  # noqa: BLE001 - тестовая заглушка не обязана знать классы SDK
            log.warning("telegram.stream_edit_failed", err=f"{type(exc).__name__}: {exc}"[:200])
            self._broken = True
            return False
        return True


def _is_noop(exc: BaseException) -> bool:
    """«message is not modified» — это не ошибка, а «уже так и стоит».

    SDK отдаёт её тем же классом, что и настоящий отказ разметки, поэтому различаем по тексту: без
    этого каждая одинаковая правка выключала бы стриминг до конца хода.
    """
    message = str(getattr(exc, "message", "") or exc).lower()
    return "not modified" in message or "do not modify" in message


def make_stream(message: Any, cfg: Any) -> TelegramStream | None:
    """Стриминг включён и живое сообщение на месте — иначе None, и путь обычный.

    Функция, а не решение внутри бота, потому что провайдерами стриминга могут стать не только
    Telegram: правило «когда рисовать негде» должно быть одно на всех.
    """
    if not getattr(cfg, "stream_replies", False) or message is None:
        return None
    return TelegramStream(
        message,
        interval_s=float(getattr(cfg, "stream_edit_interval_ms", 900)) / 1000,
    )
