"""Инструменты задач: todo-лист владельца, управляемый словами.

Та же дисциплина, что у напоминаний: дедлайн приходит дословной фразой («в пятницу
вечером») и переводится детерминированным парсером; модель не имеет права выдумывать ISO.
Задача — место для состояния («сделано/горит»), напоминание — место для звука; они связаны:
флажок remind_on_due превращает созревший дедлайн в напоминание (авто-цикл).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from aegis.agents.tools.registry import ToolContext, registry
from aegis.governance.policy import Risk
from aegis.planning.schedule import WhenNotParsed, parse_when
from aegis.planning.tasks import SqlTaskStore
from aegis.platform.config import settings

_PRIORITY = "−2 «когда будет время» … 2 «горим»"


class AddTaskArgs(BaseModel):
    title: str = Field(
        min_length=2, max_length=500, description="что сделать — одной строкой, словами владельца"
    )
    notes: str = Field(default="", max_length=2000, description="детали, ссылки, контекст")
    tags: str = Field(default="", max_length=200, description="метки через запятую: работа, дом")
    priority: int = Field(default=0, ge=-2, le=2, description=f"приоритет: {_PRIORITY}")
    due: str | None = Field(
        default=None,
        max_length=120,
        description=(
            "ДЕДЛАЙН ДОСЛОВНОЙ ФРАЗКОЙ владельца (например «в пятницу вечером», «15 сентября "
            "до обеда») — НЕ переводи в ISO; я разберу сам. Пусто — без срока"
        ),
    )
    remind: bool = Field(
        default=True,
        description="true — при наступлении дедлайна напомнить (только вместе со сроком)",
    )


class ListTasksArgs(BaseModel):
    status: str = Field(
        default="active",
        description="open|doing|done|archived|active (открытые+в работе, по умолчанию)|all",
    )
    limit: int = Field(default=20, ge=1, le=100)


class TaskRefArgs(BaseModel):
    ref: str = Field(
        min_length=1, max_length=120, description="начало id задачи или слово из заголовка"
    )


class UpdateTaskArgs(BaseModel):
    ref: str = Field(min_length=1, max_length=120, description="начало id или слово из заголовка")
    status: str | None = Field(
        default=None, description="open|doing|done|archived — смена состояния"
    )
    title: str | None = Field(default=None, max_length=500)
    priority: int | None = Field(default=None, ge=-2, le=2)
    due: str | None = Field(
        default=None,
        max_length=120,
        description="новый дедлайн дословной фразой («завтра к 18:00»); пусто — не менять",
    )
    clear_due: bool = Field(default=False, description="true — снять срок совсем")
    remind: bool | None = Field(default=None, description="true/false — напоминать о сроке")


@registry.register(
    "add_task",
    "Записать владельцу задачу в его список дел (todo). Отличается от напоминания тем, что "
    "у задачи есть состояние: её можно сделать, закрыть, вернуть в работу.",
    AddTaskArgs,
    writes=True,
    risk=Risk.LOW,
)
async def add_task(args: AddTaskArgs, ctx: ToolContext) -> str:
    cfg = settings()
    due_at = None
    if args.due:
        try:
            when = parse_when(
                args.due, now=datetime.now(UTC).astimezone(cfg.tz), timezone=cfg.timezone
            )
        except (WhenNotParsed, ValueError) as exc:
            return (
                f"Не ставлю: дедлайн «{args.due}» не разобрал — {exc}. Спроси у владельца точнее."
            )
        due_at = when.at
    if due_at is not None and due_at < datetime.now(UTC):
        past = due_at.astimezone(cfg.tz).strftime("%d.%m %H:%M")
        return f"Не ставлю: срок «{args.due}» уже прошёл ({past})."
    try:
        row = await SqlTaskStore().add(
            owner_id=ctx.owner_id,
            title=args.title,
            notes=args.notes,
            tags=args.tags,
            priority=args.priority,
            due_at=due_at,
            remind_on_due=bool(args.remind and due_at is not None),
        )
    except Exception as exc:  # noqa: BLE001
        return _db_note(exc)
    local = due_at.astimezone(cfg.tz) if due_at else None
    tail = f", срок до {local:%d.%m %H:%M}" if local else ""
    fire = " + напомню о сроке" if (local and args.remind) else ""
    return f"Задача «{row.title}» добавлена ({row.id[:8]}{tail}{fire})."


@registry.register(
    "list_tasks",
    "Показать список задач владельца: что открыто, что горит, что сделано на этой неделе.",
    ListTasksArgs,
    risk=Risk.NONE,
)
async def list_tasks(args: ListTasksArgs, ctx: ToolContext) -> str:
    cfg = settings()
    status = (args.status or "active").strip().lower()
    try:
        rows = await SqlTaskStore().list_tasks(
            owner_id=ctx.owner_id, status=None if status == "all" else status, limit=args.limit
        )
        stat = await SqlTaskStore().stats(owner_id=ctx.owner_id)
    except Exception as exc:  # noqa: BLE001
        return _db_note(exc)
    if not rows:
        return (
            f"Пусто ({status}). Открытых: {stat.get('open', 0)}, "
            f"просроченных: {stat.get('overdue', 0)}."
        )
    lines = []
    for t in rows:
        mark = {"open": "☐", "doing": "▶", "done": "☑", "archived": "·"}[t.status]
        due = ""
        if t.due_at is not None:
            loc = t.due_at.astimezone(cfg.tz)
            over = " ⏰просрочено" if t.status == "open" and t.due_at < datetime.now(UTC) else ""
            due = f" — до {loc:%d.%m %H:%M}{over}"
        pr = f" ‼{t.priority}" if t.priority > 0 else ""
        lines.append(f"{mark} {t.id[:8]} «{t.title}»{pr}{due}")
    head = (
        f"Задачи ({len(rows)}) · открыто {stat.get('open', 0)}, "
        f"горит {stat.get('overdue', 0)}, закрыто за неделю {stat.get('done_week', 0)}:"
    )
    return "\n".join([head, *lines])


@registry.register(
    "complete_task",
    "Отметить задачу выполненной. Одна попытка по ref; если не нашёл — так и скажу владельцу.",
    TaskRefArgs,
    writes=True,
    risk=Risk.LOW,
)
async def complete_task(args: TaskRefArgs, ctx: ToolContext) -> str:
    try:
        row = await SqlTaskStore().update(owner_id=ctx.owner_id, ref=args.ref, status="done")
    except Exception as exc:  # noqa: BLE001
        return _db_note(exc)
    return f"Готово: «{row.title}» закрыта ✅" if row else f"Не нашёл задачу по «{args.ref}»."


@registry.register(
    "update_task",
    "Изменить задачу: состояние (open/doing/done/archived), заголовок, приоритет, срок "
    "(дословной фразой — например для «отложи на завтра»), снять срок.",
    UpdateTaskArgs,
    writes=True,
    risk=Risk.LOW,
)
async def update_task(args: UpdateTaskArgs, ctx: ToolContext) -> str:
    cfg = settings()
    due_at = None
    if args.due:
        try:
            when = parse_when(
                args.due, now=datetime.now(UTC).astimezone(cfg.tz), timezone=cfg.timezone
            )
        except (WhenNotParsed, ValueError) as exc:
            return f"Не меняю: срок «{args.due}» не разобрал — {exc}"
        due_at = when.at
    try:
        row = await SqlTaskStore().update(
            owner_id=ctx.owner_id,
            ref=args.ref,
            status=args.status,
            title=args.title,
            priority=args.priority,
            due_at=due_at,
            clear_due=args.clear_due,
            remind_on_due=args.remind,
        )
    except Exception as exc:  # noqa: BLE001
        return _db_note(exc)
    if row is None:
        return f"Не нашёл задачу по «{args.ref}»."
    due = ""
    if row.due_at is not None:
        due = f", срок {row.due_at.astimezone(cfg.tz):%d.%m %H:%M}"
    return f"Обновил «{row.title}»: {row.status}{due} ({row.id[:8]})."


def _db_note(exc: Exception) -> str:
    return (
        "База недоступна — задача не сохранена (обычно не поднят Postgres, make up-core). "
        f"Подробность: {type(exc).__name__}: {str(exc)[:160]}"
    )
