"""Задачи владельца (``planning.tasks``): «что сделано, что горит» — без Jira-театра.

Задача живёт рядом с напоминаниями, но это не напоминание: у напоминания нет состояния,
у задачи есть. Дедлайн с remind_on_due — двусторонняя связь: авто-цикл maturity-петли
превращает созревший дедлайн в напоминание (однократно: флажок снимается тем же коммитом),
а голосовая/текстовая «сделай X к пятнице» — это task + напоминание за пару tool-вызовов.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from aegis.platform.db import session

__all__ = ["SqlTaskStore", "TaskRow"]

_STATUSES = ("open", "doing", "done", "archived")


@dataclass(slots=True)
class TaskRow:
    id: str
    owner_id: int
    title: str
    notes: str = ""
    tags: str = ""
    status: str = "open"
    priority: int = 0
    due_at: datetime | None = None
    remind_on_due: bool = False
    done_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "short_id": self.id[:8],
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
            "due_at": self.due_at.astimezone(UTC).isoformat(timespec="minutes")
            if self.due_at
            else None,
            "tags": self.tags,
        }


def _row(r: Any) -> TaskRow:
    return TaskRow(
        id=str(r["id"]),
        owner_id=int(r["owner_id"]),
        title=str(r["title"]),
        notes=str(r["notes"] or ""),
        tags=str(r["tags"] or ""),
        status=str(r["status"]),
        priority=int(r["priority"]),
        due_at=r["due_at"],
        remind_on_due=bool(r["remind_on_due"]),
        done_at=r["done_at"],
    )


_COLS = "id, owner_id, title, notes, tags, status, priority, due_at, remind_on_due, done_at"


class SqlTaskStore:
    def __init__(self, *, session_factory: Any = None) -> None:
        self._sm = session_factory

    def _session(self) -> Any:
        return self._sm() if self._sm is not None else session()

    async def add(
        self,
        *,
        owner_id: int,
        title: str,
        notes: str = "",
        tags: str = "",
        priority: int = 0,
        due_at: datetime | None = None,
        remind_on_due: bool = False,
    ) -> TaskRow:
        title = (title or "").strip()[:500]
        if not title:
            raise ValueError("пустая задача — только для collection-заговора")
        pr = max(-2, min(2, int(priority)))
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(
                            f"INSERT INTO planning.tasks (owner_id, title, notes, tags, priority,"  # noqa: S608
                            f" due_at, remind_on_due) VALUES (:o, :t, :n, :g, :p, :d, :r)"
                            f" RETURNING {_COLS}"
                        ),
                        {
                            "o": int(owner_id),
                            "t": title,
                            "n": (notes or "").strip()[:2000],
                            "g": (tags or "").lower()[:200],
                            "p": pr,
                            "d": due_at,
                            "r": bool(remind_on_due and due_at is not None),
                        },
                    )
                )
                .mappings()
                .one()
            )
            await s.commit()
        return _row(row)

    async def update(
        self,
        *,
        owner_id: int,
        ref: str,
        status: str | None = None,
        title: str | None = None,
        due_at: datetime | None = None,
        clear_due: bool = False,
        remind_on_due: bool | None = None,
        priority: int | None = None,
    ) -> TaskRow | None:
        """Частичное обновление по ref8/подстроке заголовка. None-поля не трогаем;
        done/atchievement-переход сам ставит done_at."""
        if status is not None and status not in _STATUSES:
            raise ValueError(f"status обязан быть одним из {_STATUSES}")
        sets: list[str] = ["updated_at = now()"]
        args: dict[str, Any] = {"o": int(owner_id), "ref": ref.strip()}
        if status is not None:
            sets.append("status = :st")
            args["st"] = status
            if status == "done":
                sets.append("done_at = now()")
            elif status in ("open", "doing"):
                sets.append("done_at = NULL")
        if title is not None:
            sets.append("title = :t")
            args["t"] = title.strip()[:500]
        if clear_due:
            sets.append("due_at = NULL")
            sets.append("remind_on_due = false")
        elif due_at is not None:
            sets.append("due_at = :d")
            args["d"] = due_at
        if remind_on_due is not None:
            sets.append("remind_on_due = :r")
            args["r"] = bool(remind_on_due) and not clear_due
        if priority is not None:
            sets.append("priority = :p")
            args["p"] = max(-2, min(2, int(priority)))
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(
                            f"UPDATE planning.tasks SET {', '.join(sets)} WHERE id ="  # noqa: S608
                            f" (SELECT id FROM planning.tasks WHERE owner_id = :o AND"
                            f" (id::text LIKE :ref || '%' OR lower(title) LIKE"
                            f" '%' || lower(:ref) || '%') ORDER BY (id::text LIKE :ref ||"
                            f" '%') DESC, updated_at DESC LIMIT 1) RETURNING {_COLS}"
                        ),
                        args,
                    )
                )
                .mappings()
                .first()
            )
            await s.commit()
        return _row(row) if row is not None else None

    async def list_tasks(
        self,
        *,
        owner_id: int,
        status: str | None = "open",
        limit: int = 20,
    ) -> list[TaskRow]:
        cond = ""
        if status == "active":
            cond = "AND status IN ('open', 'doing')"
        elif status in _STATUSES:
            cond = f"AND status = '{status}'"
        async with self._session() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            f"SELECT {_COLS} FROM planning.tasks WHERE owner_id = :o {cond}"  # noqa: S608
                            " ORDER BY (due_at IS NULL), due_at NULLS LAST, priority DESC,"
                            " updated_at DESC LIMIT :n"
                        ),
                        {"o": int(owner_id), "n": int(limit)},
                    )
                )
                .mappings()
                .all()
            )
        return [_row(r) for r in rows]

    async def stats(self, *, owner_id: int) -> dict[str, int]:
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "SELECT count(*) FILTER (WHERE status = 'open')::int AS open,"
                            " count(*) FILTER (WHERE status = 'doing')::int AS doing,"
                            " count(*) FILTER (WHERE status = 'open' AND due_at < now())::int AS"
                            " overdue, count(*) FILTER (WHERE status = 'done' AND done_at >"
                            " now() - interval '7 days')::int AS done_week"
                            " FROM planning.tasks WHERE owner_id = :o"
                        ),
                        {"o": int(owner_id)},
                    )
                )
                .mappings()
                .one()
            )
        return dict(row)

    async def claim_due_reminders(self, *, now: datetime, limit: int = 20) -> list[TaskRow]:
        """Созревшие дедлайны с remind_on_due; флажок снимается ЭТИМ ЖЕ коммитом —
        напоминание не протечёт и не задублируется при рестарте."""
        out: list[TaskRow] = []
        async with self._session() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            f"SELECT {_COLS} FROM planning.tasks WHERE status = 'open'"  # noqa: S608
                            " AND remind_on_due AND due_at IS NOT NULL AND due_at <= :now"
                            " ORDER BY due_at LIMIT :n FOR UPDATE SKIP LOCKED"
                        ),
                        {"now": now, "n": int(limit)},
                    )
                )
                .mappings()
                .all()
            )
            for r in rows:
                await s.execute(
                    text("UPDATE planning.tasks SET remind_on_due = false WHERE id = :i"),
                    {"i": str(r["id"])},
                )
                out.append(_row(r))
            await s.commit()
        return out
