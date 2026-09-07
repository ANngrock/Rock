"""Инструменты крон-прогонов: агент делает что-то сам по расписанию.

Это самая ответственная кнопка: «каждое утро в 8 посчитай X и пришли сводку» означает, что
модель без участия владельца запускается на его же промпт. Ограждения, которые делают это
терпимым: промпт хранится дословно (пишет владелец словами, я его не переформулирую),
доставка только владельцу, 5 падений подряд — авто-пауза, а не бесконечный стук.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from aegis.agents.tools.registry import ToolContext, registry
from aegis.governance.policy import Risk
from aegis.planning.jobs import REPEATS, SqlJobStore
from aegis.planning.schedule import WhenNotParsed, parse_when
from aegis.platform.config import settings


class NoArgs(BaseModel):
    """Пустой набор аргументов (для OpenAI-схемы важен ``type: object``)."""


class AddJobArgs(BaseModel):
    title: str = Field(min_length=3, max_length=80, description="название прогона парой слов")
    prompt: str = Field(
        min_length=3,
        max_length=4000,
        description=(
            "что делать при каждом запуске — ЗАДАЧА ЦЕЛИКОМ словами владельца (этот текст уйдёт "
            "мне же в агентный ход; пиши так, будто просишь прямо сейчас)"
        ),
    )
    repeat: str = Field(
        default="daily",
        description="once|every_min|hourly|daily|weekdays (по будням)|weekly (раз в неделю)",
    )
    at_time: str = Field(
        default="08:00",
        max_length=5,
        description="во сколько (ЧЧ:ММ по местному времени владельца) для daily/weekdays/weekly",
    )
    every_min: int = Field(
        default=30, ge=1, le=1440, description="интервал в минутах для repeat=every_min"
    )
    dow: int = Field(
        default=1, ge=1, le=7, description="день недели для weekly: 1=понедельник … 7=воскресенье"
    )
    at: str | None = Field(
        default=None,
        max_length=120,
        description=(
            "для repeat=once — дословная фраза момента («завтра в 10 утра»); НЕ переводи в ISO"
        ),
    )
    channel: str = Field(
        default="message",
        description=(
            "message — прислать результат владельцу; silent — выполнить молча (для эффектов)"
        ),
    )


class JobRefArgs(BaseModel):
    ref: str = Field(min_length=1, max_length=120, description="начало id или слово из названия")


class PauseJobArgs(BaseModel):
    ref: str = Field(min_length=1, max_length=120)
    paused: bool = Field(default=True, description="true — на паузу, false — снять с паузы")


@registry.register(
    "add_job",
    "Запланировать повторяться самому: в указанное время я (агент) выполню этот промпт и "
    "пришлю результат владельцу. Это не напоминание — я реально делаю работу без запроса.",
    AddJobArgs,
    writes=True,
    risk=Risk.MEDIUM,
)
async def add_job(args: AddJobArgs, ctx: ToolContext) -> str:
    cfg = settings()
    if not cfg.automation_enabled:
        return "Прогоны выключены (AUTOMATION_ENABLED=false) — не ставлю."
    repeat = (args.repeat or "daily").strip().lower()
    if repeat not in REPEATS:
        return f"Не ставлю: repeat обязан быть одним из {list(REPEATS)}"
    first_run = None
    if repeat == "once":
        if not args.at:
            return "Не ставлю: для once нужен момент — поле at дословной фразой."
        try:
            moment = parse_when(
                args.at, now=datetime.now(UTC).astimezone(cfg.tz), timezone=cfg.timezone
            )
        except (WhenNotParsed, ValueError) as exc:
            return f"Не ставлю: момент «{args.at}» не разобрал — {exc}"
        first_run = moment.at
    try:
        job_id = await SqlJobStore().add(
            owner_id=ctx.owner_id,
            title=args.title,
            prompt=args.prompt,
            repeat=repeat,
            at_time=args.at_time if repeat != "once" else "",
            every_min=args.every_min,
            dow=args.dow,
            channel=args.channel if args.channel in ("message", "silent") else "message",
            first_run=first_run,
            tz=cfg.tz,
        )
    except ValueError as exc:
        return f"Не ставлю: {exc}"
    except Exception as exc:  # noqa: BLE001
        return _db_note(exc)
    if first_run is not None:
        when = f"в {first_run.astimezone(cfg.tz):%d.%m %H:%M} — один раз"
    elif repeat in ("daily", "weekdays", "weekly"):
        when = f"{repeat}, в {args.at_time}"
    else:
        when = f"{repeat}, каждые {args.every_min} мин"
    return f"Прогон «{args.title.strip()}» ({job_id[:8]}): {when}. Результат — в личные сообщения."


@registry.register(
    "list_jobs",
    "Показать запланированные прогоны владельца: расписание, статус, последняя ошибка.",
    NoArgs,
    risk=Risk.NONE,
)
async def list_jobs(args: NoArgs, ctx: ToolContext) -> str:
    cfg = settings()
    try:
        jobs = await SqlJobStore().list_jobs(owner_id=ctx.owner_id)
    except Exception as exc:  # noqa: BLE001
        return _db_note(exc)
    if not jobs:
        return "Прогонов нет. «Каждое утро в 8 сделай X» — и появится первый."
    lines = []
    for j in jobs:
        state = {"active": "▶", "paused": "⏸", "done": "✔"}[j.status]
        nxt = (
            f" · далее {j.next_run.astimezone(cfg.tz):%d.%m %H:%M}"
            if j.next_run and j.status == "active"
            else ""
        )
        err = f" · ошибок {j.fail_count}: «{j.last_error[:60]}»" if j.last_error else ""
        shown = f" в {j.at_time}" if j.at_time else ""
        lines.append(f"{state} {j.id[:8]} «{j.title}» — {j.repeat}{shown}{nxt}{err}")
    return "Прогоны:\n" + "\n".join(lines)


@registry.register(
    "run_job_now",
    "Поставить прогон в очередь на ближайший тик (минута). Не «выполнено сразу» — честно.",
    JobRefArgs,
    writes=True,
    risk=Risk.MEDIUM,
)
async def run_job_now(args: JobRefArgs, ctx: ToolContext) -> str:
    try:
        note = await SqlJobStore().trigger_now(owner_id=ctx.owner_id, ref=args.ref)
    except Exception as exc:  # noqa: BLE001
        return _db_note(exc)
    return note or f"Не нашёл прогон по «{args.ref}»."


@registry.register(
    "pause_job",
    "Поставить прогон на паузу или снять с неё.",
    PauseJobArgs,
    writes=True,
    risk=Risk.LOW,
)
async def pause_job(args: PauseJobArgs, ctx: ToolContext) -> str:
    try:
        note = await SqlJobStore().set_status(
            owner_id=ctx.owner_id, ref=args.ref, status="paused" if args.paused else "active"
        )
    except Exception as exc:  # noqa: BLE001
        return _db_note(exc)
    return note or f"Не нашёл прогон по «{args.ref}» (или он уже в таком состоянии)."


@registry.register(
    "remove_job",
    "Удалить прогон совсем (история его запусков остаётся в журнале).",
    JobRefArgs,
    writes=True,
    risk=Risk.LOW,
)
async def remove_job(args: JobRefArgs, ctx: ToolContext) -> str:
    try:
        ok = await SqlJobStore().drop(owner_id=ctx.owner_id, ref=args.ref)
    except Exception as exc:  # noqa: BLE001
        return _db_note(exc)
    return "Удалил." if ok else f"Не нашёл прогон по «{args.ref}»."


def _db_note(exc: Exception) -> str:
    return (
        "База недоступна — прогон не сохранен (обычно не поднят Postgres, make up-core). "
        f"Подробность: {type(exc).__name__}: {str(exc)[:160]}"
    )
