"""Инструменты наблюдений: «следи, пока не случится» словами владельца.

Слово в слово та же философия, что у напоминаний: модель переводит формулировку в аргументы,
а время и условие проверяет детерминированный код. Отдельно подчеркнуто в описании set_watch:
через Telegram бот звонить не умеет в принципе (Bot API не даёт), канал `call` — это телефонный
провайдер; модель обязана сказать владельцу правду, а не пообещать «позвоню в телегу».
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from aegis.agents.tools.registry import ToolContext, registry
from aegis.governance.policy import Risk
from aegis.planning.reminders import REMINDER_CHANNELS
from aegis.planning.schedule import WhenNotParsed, parse_when
from aegis.planning.watchers import SqlWatchStore
from aegis.platform.config import settings


class NoArgs(BaseModel):
    """Пустой набор аргументов (для OpenAI-схемы важен ``type: object``)."""


class SetWatchArgs(BaseModel):
    title: str = Field(
        min_length=3,
        max_length=200,
        description="что это за наблюдение — одна строла владельцу (его словами)",
    )
    kind: str = Field(description="page — следить за страницей; search — следить за выдачей поиска")
    target: str = Field(
        min_length=4,
        max_length=2000,
        description="для page — полный http(s) URL; для search — запрос",
    )
    mode: str = Field(
        default="contains",
        description=(
            "contains — дождаться подстроки; regex — дождаться совпадения с образцом; "
            "changed — любое изменение страницы (только для page)"
        ),
    )
    needle: str | None = Field(
        default=None,
        max_length=400,
        description="что искать (для contains/regex); для changed не заполняется",
    )
    every_minutes: int = Field(default=15, ge=5, le=14400)
    channel: str = Field(
        default="message",
        description=(
            "как сообщить о наступлении: message — сообщением (по умолчанию), call — звонком на "
            "настроенный телефон, both — обоим. ВНИМАНИЕ: звонков ВНУТРИ Telegram не существует "
            "(Bot API не умеет звонить) — если владелец просит «позвони в телеге», ставь message "
            "и объясни, что доступный «звонок» — это звонок на номер из настроек"
        ),
    )
    until: str | None = Field(
        default=None,
        max_length=120,
        description="дословная фраза «до скольки» (например «до пятницы»); пусто — без горизонта",
    )
    repeat: bool = Field(
        default=False,
        description=(
            "true — напоминать при каждом попадании (цена упала), false — сработать один раз"
        ),
    )


class WatchRefArgs(BaseModel):
    ref: str = Field(
        min_length=2,
        max_length=120,
        description="начало id наблюдения или слово из его названия",
    )


@registry.register(
    "set_watch",
    "Начать следить за событием: страница/поиск проверяются по расписанию, при наступлении "
    "условия владельцу приходит напоминание (сообщением или звонком). Время и условие задаёт "
    "владелец — передавай их дословно.",
    SetWatchArgs,
    writes=True,
    risk=Risk.LOW,
)
async def set_watch(args: SetWatchArgs, ctx: ToolContext) -> str:
    cfg = settings()
    if not cfg.reminders_enabled:
        return (
            "Наблюдения выключены (REMINDERS_ENABLED=false): весть доставляется напоминанием, "
            "а напоминания выключены — включать наблюдение втихую я не буду."
        )
    store = SqlWatchStore()
    expires_at = None
    until_note = ""
    if args.until:
        try:
            moment = parse_when(
                args.until.replace("до ", "", 1) if args.until.startswith("до ") else args.until,
                now=datetime.now(UTC).astimezone(cfg.tz),
                timezone=cfg.timezone,
            )
            expires_at = moment.at
        except WhenNotParsed as exc:
            return f"Не ставлю: горизонт «{args.until}» не разобрал — {exc}"
    channel = (args.channel or "message").strip().lower()
    if channel not in REMINDER_CHANNELS:
        return f"Не ставлю: канал доставки должен быть одним из {list(REMINDER_CHANNELS)}"
    call_ready = (cfg.call_provider or "none").strip().lower() != "none" and bool(
        (cfg.notify_phone or "").strip()
    )
    warning = ""
    if channel in ("call", "both") and not call_ready:
        warning = "\n⚠️ звонки не настроены — весть придёт сообщением; заказ звонка сохранён."
    if channel in ("call", "both"):
        warning += (
            "\n(Напоминаю честно: звонков внутри Telegram нет — это звонок на номер из настроек.)"
        )
    kind = (args.kind or "").strip().lower()
    if kind not in ("page", "search"):
        return "Не ставлю: kind — только page (страница) или search (выдача поиска)"
    try:
        watch_id = await store.add(
            owner_id=ctx.owner_id,
            title=args.title,
            kind=kind,
            target=args.target,
            mode=args.mode,
            needle=args.needle,
            interval_minutes=args.every_minutes,
            channel=channel,
            repeat=args.repeat,
            expires_at=expires_at,
            trace_id=ctx.trace_id,
        )
    except ValueError as exc:
        return f"Не ставлю: {exc}"
    except Exception as exc:  # noqa: BLE001 - «начал следить» без записи было бы враньём
        return f"Не сохранилось: {type(exc).__name__}: {str(exc)[:200]}"
    local = expires_at.astimezone(cfg.tz) if expires_at is not None else None
    head = (
        f"Слежу: {args.title} — {kind}, каждые {args.every_minutes} мин, "
        f"условие «{args.mode}{f': {args.needle}' if args.needle else ''}»"
        + (f", до {local:%d.%m %H:%M}" if local else "")
        + ("; повторяемое" if args.repeat else "; сработает один раз")
    )
    return (
        f"{head}. id={watch_id[:8]}.{until_note}{warning}"
        f"\nОтмена — cancel_watch с ref={watch_id[:8]}."
    )


@registry.register(
    "list_watch",
    "Показать активные и приостановленные наблюдения владельца (интервалы, условия, ошибки).",
    NoArgs,
)
async def list_watch(args: NoArgs, ctx: ToolContext) -> str:
    store = SqlWatchStore()
    try:
        rows = await store.list_active(owner_id=ctx.owner_id)
    except Exception as exc:  # noqa: BLE001 - список не должен ронять ответ
        return f"Не получилось прочитать список: {type(exc).__name__}"
    if not rows:
        return "Активных наблюдений нет."
    cfg = settings()
    lines = []
    for row in rows:
        mark = "►" if row["status"] == "active" else "⏸ на паузе"
        due = row["fire_at"]
        due_local = due.astimezone(cfg.tz) if due.tzinfo else due
        cond = row["mode"] + (f" «{row['needle']}»" if row["needle"] else "")
        lines.append(
            f"{mark} {str(row['id'])[:8]} · {row['title']} — {row['kind']}: {cond}"
            f", каждые {row['interval_minutes']} мин, след. проверка {due_local:%d.%m %H:%M}"
        )
        if row.get("last_error"):
            lines.append(f"   ! {str(row['last_error'])[:120]}")
    return "\n".join(lines)


@registry.register(
    "cancel_watch",
    "Отменить или приостановить наблюдение (pause/resume — отдельной командой владельца не делаю:"
    " отменяю совсем, владелец может попросить паузу словами).",
    WatchRefArgs,
)
async def cancel_watch(args: WatchRefArgs, ctx: ToolContext) -> str:
    store = SqlWatchStore()
    try:
        outcome = await store.set_status(owner_id=ctx.owner_id, ref=args.ref, status="cancel")
    except Exception as exc:  # noqa: BLE001
        return f"Не отменилось: {type(exc).__name__}"
    if outcome is None:
        return "Не нашёл активное наблюдение по этому ref/слову — список покажет, что есть."
    return f"Отменено: {outcome}"
