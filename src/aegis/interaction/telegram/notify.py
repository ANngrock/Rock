"""Доставка напоминаний владельцу (шаг 2) — отдельный маленький модуль, а не часть ``bot.py``.

Причина разбиения: ``bot.py`` живёт в polling-цикле и тянет за собой диспетчер, роутеры и
клавиатуры. Тикеру же нужно ровно одно — уметь отправить сообщение и корректно умереть, если
Telegram недоступен. Держать этот путь в одном файле с ботом значило бы либо поднимать ради двух
строк весь бот, либо копировать ``send_message`` в CLI.

Отдаём **plain text без parse_mode**: текст напоминания придумал не мы (владелец мог вставить в него
``<b>`` из письма), а падение разбора разметки в 3:07 — самый плохой способ обнаружить, что
напоминания не работают.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from aegis.planning.reminders import DELIVER_PREFIX, Reminder
from aegis.platform.config import Settings

__all__ = ["TelegramNotifier", "print_reminder"]

log = structlog.get_logger(__name__)

#: Telegram режет сообщения длиннее 4096 знаков; берём с запасом — лучше хвост, чем ошибка отправки
_MAX_LEN = 3800


@dataclass(slots=True)
class TelegramNotifier:
    """Один чат, один бот, никаких состояний: доставил или raised — третьего не дано."""

    token: str
    chat_id: int
    _bot: Any = field(default=None, repr=False)

    @classmethod
    def from_settings(cls, cfg: Settings) -> TelegramNotifier:
        token = cfg.telegram_bot_token.get_secret_value() if cfg.telegram_bot_token else ""
        if not token or cfg.telegram_owner_id is None:
            raise RuntimeError("нужны TELEGRAM_BOT_TOKEN и TELEGRAM_OWNER_ID: некуда доставлять")
        # для личного чата chat_id совпадает с user id — отдельного ключа нет намеренно
        return cls(token=token, chat_id=int(cfg.telegram_owner_id))

    async def start(self) -> None:
        if self._bot is not None:
            return
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties

        self._bot = Bot(token=self.token, default=DefaultBotProperties(parse_mode=None))

    async def aclose(self) -> None:
        if self._bot is not None:
            await self._bot.session.close()
            self._bot = None

    async def send(self, reminder: Reminder) -> None:
        await self.start()
        body = " ".join(reminder.text.split())[:_MAX_LEN]
        text = f"{DELIVER_PREFIX} {body}"
        try:
            await self._bot.send_message(self.chat_id, text, link_preview_options=None)
        except Exception as exc:  # noqa: BLE001 - отдаём наверх: попытку считает магазин
            log.warning("reminder.send_failed", id=reminder.short_id, err=repr(exc)[:200])
            raise
        log.info("reminder.sent", id=reminder.short_id, chat=self.chat_id)


async def print_reminder(reminder: Reminder) -> None:
    """Отправка «в консоль» для ``--dry-run``: показать, что ушло бы, не трогая Telegram."""
    print(f"  chat {reminder.owner_id}: {DELIVER_PREFIX} {reminder.text[:200]}")
