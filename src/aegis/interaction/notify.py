"""Диспетчер каналов доставки напоминаний (миграция 0009): тик зовёт один метод — решают каналы.

Порядок для `both` выбран по цене ошибки: сначала сообщение (дешёвое, надёжное), потом звонок.
Если сообщение не ушло — звонка не будет вовсе: попытка повторит весь тик, а «дозвонились, но
строка не помечена» означает повторный звонок среди ночи. Для `call` наоборот: звонок — обещание,
сообщение — способ сказать «не дозвонился». Отказ звонка не превращает доставку в провал —
владелец получил текст и знает, что не получил звонок; это деградация, а не потеря.

Молчание запрещено контрактом: любой fallback оставляет строку в ``last_notes`` (её печатает
отчёт тика) и запись в логе. «Напоминание как-то доставили, но не так, как просили» — факт,
который владелец обязан видеть, а не находить в логах.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC
from zoneinfo import ZoneInfo

import structlog

from aegis.interaction.calls import CallError, CallProvider, call_provider_from_settings
from aegis.interaction.telegram.notify import TelegramNotifier
from aegis.planning.reminders import Reminder
from aegis.platform.config import Settings

__all__ = ["ReminderDispatcher"]

log = structlog.get_logger(__name__)

_MONTHS_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def spoken_text(reminder: Reminder, timezone: str | None = None) -> str:
    """Что произнесёт TTS. Дата прописью и «N часов» — не косметика: «15:00» русские голоса
    читают то «пятнадцать ноль ноль», то «три часа дня», а напоминание — не место для фантазии
    синтезатора о формате."""
    zone = ZoneInfo(timezone) if timezone else UTC
    when = reminder.due_at.astimezone(zone) if reminder.due_at.tzinfo else reminder.due_at
    hh = when.hour
    mm = f" {when.minute} минут" if when.minute else ""
    body = " ".join(str(reminder.text).split())[:500]
    return (
        f"Айджис, напоминание: {body}. "
        f"Это на {when.day} {_MONTHS_GENITIVE[when.month - 1]}, {hh} часов{mm}."
    )


@dataclass(slots=True)
class ReminderDispatcher:
    """Один ``send(reminder)`` на все каналы: tикеру (CLI и бот-процесс) не знать про провайдеры."""

    telegram: TelegramNotifier
    calls: CallProvider | None = None
    phone: str = ""
    timezone: str = "UTC"
    #: «звонок не удался / не настроен» для отчёта тика — молча глотать fallback нельзя
    last_notes: list[str] = field(default_factory=list)

    @classmethod
    def from_settings(cls, cfg: Settings) -> ReminderDispatcher:
        # RuntimeError (нет токена) — ответственность вызывающего: tick печатал её и раньше
        telegram = TelegramNotifier.from_settings(cfg)
        calls = call_provider_from_settings(cfg)
        return cls(
            telegram=telegram,
            calls=calls,
            phone=(cfg.notify_phone or "").strip(),
            timezone=cfg.timezone,
        )

    async def start(self) -> None:
        await self.telegram.start()

    async def aclose(self) -> None:
        await self.telegram.aclose()

    async def send(self, reminder: Reminder) -> None:
        self.last_notes = []
        want_call = reminder.channel in ("call", "both")
        if want_call and self.calls is None:
            await self.telegram.send(
                reminder,
                prefix="📞⇝ Напоминаю текстом (звонки не настроены, см. CALL_PROVIDER):",
            )
            self.last_notes.append(f"{reminder.short_id}: звонок заказан, но не настроен")
            return
        if reminder.channel in ("message", "both"):
            await self.telegram.send(reminder)
        if want_call:
            try:
                await self._call(reminder)
            except CallError as exc:
                if reminder.channel == "call":
                    await self.telegram.send(reminder, prefix="📞 Не дозвонился, пишу текстом:")
                self.last_notes.append(f"{reminder.short_id}: звонок не удался ({str(exc)[:120]})")
                log.warning("reminder.call_failed", id=reminder.short_id, err=repr(exc)[:200])

    async def _call(self, reminder: Reminder) -> None:
        provider = self.calls
        assert provider is not None  # вызывается только когда want_call и канал выбран
        await provider.call(self.phone, spoken_text(reminder, self.timezone))
