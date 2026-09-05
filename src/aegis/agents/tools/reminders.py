"""Инструменты напоминаний (шаг 2): поставить, посмотреть, отменить.

Здесь нет ни SQL, ни разбора времени: ``planning.schedule`` считает «когда», ``planning.reminders``
хранит «что». Задача этого модуля — перевести слова владельца в вызов магазина и вернуть текст, по
которому модель сможет ответить, не досочиняя даты.

Почему «когда» принимает сырая фраза, а не ISO-метка от модели: ``2026-09-06T09:00Z`` из mouths
модели — это галлюцинация с красивым фасадом. Модель обязана передать слова владельца как есть;
парсер либо согласуется с ними, либо честно отвечает «не понимаю».
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from aegis.agents.tools.registry import ToolContext, registry
from aegis.governance.policy import Risk
from aegis.planning.reminders import Reminder
from aegis.planning.schedule import When, WhenNotParsed, humanize, parse_when
from aegis.platform.config import settings

__all__ = ["cancel_reminder", "list_reminders", "set_reminder"]


class NoArgs(BaseModel):
    """Пустой набор аргументов (для OpenAI-схемы важен ``type: object``)."""


class SetReminderArgs(BaseModel):
    text: str = Field(
        min_length=3,
        max_length=500,
        description="что напомнить — коротко, в той формулировке, в которой просил владелец",
    )
    when: str = Field(
        min_length=2,
        max_length=120,
        description=(
            "дословная фраза владельца про время: «через 20 минут», «завтра в 9», "
            "«в пятницу утром», «12.09 в 14:00». Не переводить в ISO и не додумывать"
        ),
    )


class CancelArgs(BaseModel):
    ref: str = Field(
        min_length=1,
        max_length=200,
        description="начало id из списка («6f2c9a1») или любое слово из текста напоминания",
    )


class ListArgs(BaseModel):
    limit: int = Field(default=10, ge=1, le=50)


@registry.register(
    "set_reminder",
    "Поставить владельцу напоминание на указанный момент. Время передаётся словами владельца.",
    SetReminderArgs,
    writes=True,
    risk=Risk.LOW,
)
async def set_reminder(args: SetReminderArgs, ctx: ToolContext) -> str:
    cfg = settings()
    store = _store(ctx)
    if store is None:
        return _no_db()
    if not cfg.reminders_enabled:
        return "Напоминания выключены (REMINDERS_ENABLED=false) — ничего не ставлю."
    try:
        when = _resolve_when(args, cfg)
    except WhenNotParsed as exc:
        return f"Не ставлю: {exc}"
    except ValueError as exc:
        return f"Не ставлю: {exc}"
    try:
        reminder_id = await store.add(
            owner_id=ctx.owner_id, body=args.text, due_at=when.at, trace_id=ctx.trace_id
        )
    except Exception as exc:  # noqa: BLE001 - враньё «поставил» опаснее сообщения об ошибке
        return f"Не сохранилось: {type(exc).__name__}: {str(exc)[:200]}"
    # абсолютный момент + «через сколько»: «через 19 мин» без часов — ответ про арифметику,
    # а владельцу нужно знать, на которую минуту он рассчитывает
    local = when.at.astimezone(cfg.tz)
    moment = humanize(when.at, now=datetime.now(cfg.tz), timezone=cfg.timezone)
    note = f"\n{when.note}" if when.note else ""
    return (
        f"Поставлено на {local:%d.%m %H:%M} ({moment}) — {args.text}. id={reminder_id[:8]}.{note}\n"
        f"Отмена — инструмент cancel_reminder с ref={reminder_id[:8]}."
    )


@registry.register(
    "list_reminders",
    "Показать запланированные напоминания владельца по возрастанию времени.",
    ListArgs,
)
async def list_reminders(args: ListArgs, ctx: ToolContext) -> str:
    cfg = settings()
    store = _store(ctx)
    if store is None:
        return _no_db()
    items: list[Reminder] = await store.list_scheduled(owner_id=ctx.owner_id, limit=args.limit)
    if not items:
        return "Напоминаний не запланировано."
    now = datetime.now(cfg.tz)
    lines = []
    for reminder in items:
        local = reminder.due_at.astimezone(cfg.tz)
        when = humanize(reminder.due_at, now=now, timezone=cfg.timezone)
        warn = (
            f" — попыток {reminder.attempts}: {reminder.last_error[:80]}"
            if reminder.last_error
            else ""
        )
        lines.append(
            f"- {reminder.short_id} · {local:%d.%m %H:%M} ({when}) · {reminder.text}{warn}"
        )
    return "\n".join(lines)


@registry.register(
    "cancel_reminder",
    "Отменить напоминание по id из списка или по слову из его текста.",
    CancelArgs,
    writes=True,
    risk=Risk.LOW,
)
async def cancel_reminder(args: CancelArgs, ctx: ToolContext) -> str:
    store = _store(ctx)
    if store is None:
        return _no_db()
    removed = await store.cancel(owner_id=ctx.owner_id, ref=args.ref)
    if removed is None:
        return (
            f"Ничего не отменил: напоминание, похожее на {args.ref!r}, не найдено "
            "(смотрим только scheduled; список — list_reminders)."
        )
    return f"Отменено: {removed.text} (id={removed.short_id})."


# ------------------------------------------------------------- внутреннее --


def _store(ctx: ToolContext) -> Any:
    services = getattr(ctx, "services", None)
    store = getattr(services, "reminders", None)
    if store is None or not getattr(store, "enabled", False):
        return None
    return store


def _no_db() -> str:
    return (
        "База недоступна: напоминание некуда сохранить, поэтому я его не ставлю. "
        "Обычно это значит, что не поднят Postgres (make up-core) — без хранилища расписание "
        "прожило бы ровно до рестарта процесса."
    )


def _resolve_when(args: SetReminderArgs, cfg: Any) -> When:
    """Фраза владельца → момент. Разбирает код, не модель (см. docs/ADR/0010)."""
    local = datetime.now(cfg.tz)
    when = parse_when(args.when, now=local, timezone=cfg.timezone)
    # проверка «в будущем» уже внутри parse_when; здесь только приводим к UTC для БД
    return When(at=when.at.astimezone(UTC), matched=when.matched, note=when.note)
