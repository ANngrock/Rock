"""Хранилище источников и предметов парсера. Тело предмета — под vault, если он есть.

Поведенческие обещания, завязанные на схему (миграция 0015):

* UNIQUE(source_id, fingerprint) — перепубликованная лента не удваивает новости: «новых» просто
  не появляется, тик это только фиксирует;
* claim_due с FOR UPDATE SKIP LOCKED — бот-цикл и ``aegis feed run`` могут жить рядом;
* ``body`` — запечатанный конверт vault (префикс aeg1s:) либо открытый текст, когда ключарки нет
  (dev/crypto off): читатель обязан уметь и то, и другое, включение шифрования задним числом не
  ломает прочитанное;
* excerpt живёт открытым всегда: превью меню и поиска того требует, секрета в нём нет.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import orjson
from sqlalchemy import text

from aegis.platform.db import session

__all__ = ["ParseItem", "ParsingStore", "SqlParsingStore", "SourceRow"]

KINDS = ("auto", "web", "rss", "atom", "jsonfeed", "tg_channel")


@dataclass(slots=True)
class SourceRow:
    id: str
    owner_id: int
    kind: str
    target: str
    label: str
    interval_sec: int
    enabled: bool = True
    include_kw: str = ""
    exclude_kw: str = ""
    last_check: datetime | None = None
    last_ok: datetime | None = None
    last_error: str = ""
    cursor: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ParseItem:
    id: int
    source_id: str
    title: str
    url: str
    author: str
    excerpt: str
    body: str  # расшифрованное тело (запечатанное магазин не отдаёт наружу)
    media: list[str]
    published_at: datetime | None
    status: str
    first_seen: datetime


def _label_of(target: str) -> str:
    tail = re.split(r"[/?]", target.strip().rstrip("/"))[-1] or target
    return tail.replace("@", "")[:80] or "источник"


def _item(row: Any, body: str) -> ParseItem:
    return ParseItem(
        id=int(row["id"]),
        source_id=str(row["source_id"]),
        title=str(row["title"] or ""),
        url=str(row["url"] or ""),
        author=str(row["author"] or ""),
        excerpt=str(row["excerpt"] or ""),
        body=body,
        media=list(row["media"] or []),
        published_at=row["published_at"],
        status=str(row["status"]),
        first_seen=row["first_seen"],
    )


class ParsingStore(Protocol):
    """Порт для движка и инструментов: SQL-реализация ниже, двойник — в тестах."""

    async def add_source(
        self,
        *,
        owner_id: int,
        target: str,
        kind: str = "auto",
        label: str = "",
        interval_sec: int = 900,
        include_kw: str | None = None,
        exclude_kw: str | None = None,
    ) -> str: ...

    async def list_sources(self, *, owner_id: int, limit: int = 20) -> list[SourceRow]: ...

    async def set_enabled(self, *, owner_id: int, ref: str, enabled: bool) -> str | None: ...

    async def drop(self, *, owner_id: int, ref: str) -> bool: ...

    async def set_kw(
        self,
        *,
        owner_id: int,
        ref: str,
        include_kw: str | None,
        exclude_kw: str | None,
    ) -> bool: ...

    async def touch_run(
        self, source_id: str, *, error: str = "", cursor: dict[str, Any] | None = None
    ) -> None: ...

    async def due_sources(self, *, limit: int = 8) -> list[SourceRow]: ...

    async def add_items(
        self, *, source: SourceRow, items: list[dict[str, Any]], cipher: Any
    ) -> int: ...

    async def list_items(
        self,
        *,
        owner_id: int,
        source_ref: str | None = None,
        query: str | None = None,
        status: str | None = None,
        limit: int = 20,
        cipher: Any = None,
    ) -> list[ParseItem]: ...

    async def unread(self, *, owner_id: int) -> int: ...

    async def mark_read(self, *, owner_id: int, source_ref: str | None = None) -> int: ...

    async def mark_pushed(self, *, ids: list[int]) -> None: ...

    async def prune(self, *, keep_days: int) -> int: ...


class SqlParsingStore:
    def __init__(self, *, session_factory: Any = None) -> None:
        self._sm = session_factory

    def _session(self) -> Any:
        return self._sm() if self._sm is not None else session()

    async def add_source(
        self,
        *,
        owner_id: int,
        target: str,
        kind: str = "auto",
        label: str = "",
        interval_sec: int = 900,
        include_kw: str | None = None,
        exclude_kw: str | None = None,
    ) -> str:
        target = str(target).strip()
        if kind not in KINDS:
            raise ValueError(f"kind обязан быть одним из {KINDS}")
        if not re.match(r"^https?://\S+$", target) and not re.match(
            r"^@?[A-Za-z][A-Za-z0-9_]{3,63}$", target
        ):
            raise ValueError("источник — http(s)-URL или @имя_публичного_канала")
        if int(interval_sec) < 30:  # noqa: PLR2004 — быстрее получаса Telegram не прощает
            raise ValueError("интервал не меньше 30 секунд")
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "INSERT INTO parsing.sources (owner_id, kind, target, label,"
                            " interval_sec, include_kw, exclude_kw)"
                            " VALUES (:o, :k, :t, :l, :i, :inc, :exc)"
                            " ON CONFLICT (owner_id, kind, target) DO UPDATE SET enabled = true,"
                            " updated_at = now() RETURNING id::text AS id"
                        ),
                        {
                            "o": int(owner_id),
                            "k": kind,
                            "t": target,
                            "l": (label.strip() or _label_of(target))[:80],
                            "i": int(interval_sec),
                            "inc": (include_kw or "")[:200] or None,
                            "exc": (exclude_kw or "")[:200] or None,
                        },
                    )
                )
                .mappings()
                .one()
            )
            await s.commit()
        return str(row["id"])

    async def list_sources(self, *, owner_id: int, limit: int = 20) -> list[SourceRow]:
        async with self._session() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            "SELECT id::text AS id, owner_id, kind, target, label, interval_sec,"
                            " enabled, include_kw, exclude_kw, last_check, last_ok, last_error,"
                            " cursor FROM parsing.sources"
                            " WHERE owner_id = :o ORDER BY created_at DESC LIMIT :n"
                        ),
                        {"o": int(owner_id), "n": int(limit)},
                    )
                )
                .mappings()
                .all()
            )
        return [_source(r) for r in rows]

    async def set_enabled(self, *, owner_id: int, ref: str, enabled: bool) -> str | None:
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "UPDATE parsing.sources SET enabled = :e, updated_at = now()"
                            " WHERE owner_id = :o AND id::text LIKE :ref || '%' RETURNING target"
                        ),
                        {"o": int(owner_id), "e": bool(enabled), "ref": str(ref)},
                    )
                )
                .mappings()
                .first()
            )
            await s.commit()
        return str(row["target"]) if row else None

    async def drop(self, *, owner_id: int, ref: str) -> bool:
        async with self._session() as s:
            res = await s.execute(
                text(
                    "DELETE FROM parsing.sources WHERE owner_id = :o AND id::text LIKE :ref || '%'"
                ),
                {"o": int(owner_id), "ref": str(ref)},
            )
            await s.commit()
        return bool(res.rowcount)

    async def set_kw(
        self, *, owner_id: int, ref: str, include_kw: str | None, exclude_kw: str | None
    ) -> bool:
        async with self._session() as s:
            res = await s.execute(
                text(
                    "UPDATE parsing.sources SET include_kw = :inc, exclude_kw = :exc,"
                    " updated_at = now() WHERE owner_id = :o AND id::text LIKE :ref || '%'"
                ),
                {
                    "inc": (include_kw or "")[:200] or None,
                    "exc": (exclude_kw or "")[:200] or None,
                    "o": int(owner_id),
                    "ref": str(ref),
                },
            )
            await s.commit()
        return bool(res.rowcount)

    async def touch_run(
        self, source_id: str, *, error: str = "", cursor: dict[str, Any] | None = None
    ) -> None:
        async with self._session() as s:
            await s.execute(
                text(
                    "UPDATE parsing.sources SET last_check = now(), last_error = :err,"
                    " last_ok = CASE WHEN :err = '' THEN now() ELSE last_ok END,"
                    " cursor = COALESCE(CAST(:cur AS jsonb), cursor) WHERE id = CAST(:id AS uuid)"
                ),
                {
                    "err": error[:400],
                    "cur": orjson.dumps(cursor).decode() if cursor else None,
                    "id": source_id,
                },
            )
            await s.commit()

    async def due_sources(self, *, limit: int = 8) -> list[SourceRow]:
        """Созревшие источники одного тика: SKIP LOCKED — чтобы CLI и бот не дрались."""
        async with self._session() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            "SELECT id, owner_id, kind, target, label, interval_sec, enabled,"
                            " include_kw, exclude_kw, last_check, last_ok, last_error, cursor"
                            " FROM parsing.sources"
                            " WHERE enabled AND (last_check IS NULL OR (last_check"
                            " + make_interval(secs => interval_sec)) <= now())"
                            " ORDER BY last_check NULLS FIRST LIMIT :n FOR UPDATE SKIP LOCKED"
                        ),
                        {"n": int(limit)},
                    )
                )
                .mappings()
                .all()
            )
            await s.commit()
        return [_source(r) for r in rows]

    async def add_items(
        self, *, source: SourceRow, items: list[dict[str, Any]], cipher: Any
    ) -> int:
        """Новые предметы; тело — запечатанное, когда cipher есть. Возвращает число новых."""
        from aegis.platform.vault import seal_text

        new = 0
        async with self._session() as s:
            for it in items:
                body = str(it.get("text") or "")[:20_000]
                sealed = seal_text(cipher, body) if cipher is not None else body
                res = await s.execute(
                    text(
                        "INSERT INTO parsing.items (source_id, owner_id, fingerprint, url, title,"
                        " author, excerpt, body, body_sealed, media, published_at, status)"
                        " VALUES (CAST(:sid AS uuid), :o, :fp, :url, :title, :author, :exc,"
                        " :body, :sealed, CAST(:media AS jsonb), :pub, 'new')"
                        " ON CONFLICT (source_id, fingerprint) DO NOTHING RETURNING id"
                    ),
                    {
                        "sid": source.id,
                        "o": source.owner_id,
                        "fp": str(it.get("fingerprint") or ""),
                        "url": str(it.get("url") or "")[:2000],
                        "title": str(it.get("title") or "")[:300],
                        "author": str(it.get("author") or "")[:200],
                        "exc": str(it.get("excerpt") or body[:280])[:400],
                        "body": sealed,
                        "sealed": cipher is not None and sealed != body,
                        "media": orjson.dumps(list(it.get("media") or [])[:12]).decode(),
                        "pub": it.get("published_at"),
                    },
                )
                if res.rowcount:
                    new += 1
            await s.commit()
        return new

    async def list_items(
        self,
        *,
        owner_id: int,
        source_ref: str | None = None,
        query: str | None = None,
        status: str | None = None,
        limit: int = 20,
        cipher: Any = None,
    ) -> list[ParseItem]:
        from aegis.platform.vault import open_text

        cond = " WHERE i.owner_id = :o"
        args: dict[str, Any] = {"o": int(owner_id), "n": int(limit)}
        if source_ref:
            cond += " AND s.id::text LIKE :ref || '%'"
            args["ref"] = source_ref
        if query:
            cond += " AND (i.title ILIKE :q OR i.excerpt ILIKE :q)"
            args["q"] = f"%{query[:80]}%"
        if status:
            cond += " AND i.status = :st"
            args["st"] = status
        async with self._session() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            "SELECT i.id, i.source_id::text, i.title, i.url, i.author, i.excerpt,"  # noqa: S608
                            " i.body, i.media, i.published_at, i.status, i.first_seen"
                            " FROM parsing.items i JOIN parsing.sources s ON s.id = i.source_id"
                            + cond
                            + " ORDER BY i.first_seen DESC LIMIT :n"
                        ),
                        args,
                    )
                )
                .mappings()
                .all()
            )
        out: list[ParseItem] = []
        for r in rows:
            body = str(r["body"] or "")
            try:
                body = open_text(cipher, body)
            except (ValueError, RuntimeError):
                body = ""  # нет ключа — показываем карточку без тела: заголовок и выдержка открыты
            out.append(_item(r, body))
        return out

    async def unread(self, *, owner_id: int) -> int:
        async with self._session() as s:
            n = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM parsing.items WHERE owner_id = :o AND status = 'new'"
                    ),
                    {"o": int(owner_id)},
                )
            ).scalar()
        return int(n or 0)

    async def mark_read(self, *, owner_id: int, source_ref: str | None = None) -> int:
        q = (
            "UPDATE parsing.items SET status = 'read' WHERE owner_id = :o"
            " AND status IN ('new','pushed')"
        )
        args: dict[str, Any] = {"o": int(owner_id)}
        if source_ref:
            q += " AND source_id::text LIKE :ref || '%'"
            args["ref"] = source_ref
        async with self._session() as s:
            res = await s.execute(text(q), args)
            await s.commit()
        return int(res.rowcount or 0)

    async def mark_pushed(self, *, ids: list[int]) -> None:
        if not ids:
            return
        async with self._session() as s:
            for i in ids:
                await s.execute(
                    text(
                        "UPDATE parsing.items SET status = 'pushed' WHERE id = :i AND"
                        " status = 'new'"
                    ),
                    {"i": int(i)},
                )
            await s.commit()

    async def prune(self, *, keep_days: int) -> int:
        """Старые предметы — до выдержки: тело дольше PARSER_KEEP_DAYS не держим, это архив,
        а не склад. Возвращает число «выпотрошенных» (полностью удалённых — нет)."""
        async with self._session() as s:
            res = await s.execute(
                text(
                    "UPDATE parsing.items SET body = '', body_sealed = false WHERE first_seen <"
                    " now() - make_interval(days => :d) AND body <> ''"
                ),
                {"d": int(keep_days)},
            )
            await s.commit()
        return int(res.rowcount or 0)


def _source(r: Any) -> SourceRow:
    cur = r["cursor"]
    if isinstance(cur, str):
        import orjson

        cur = orjson.loads(cur)
    return SourceRow(
        id=str(r["id"]),
        owner_id=int(r["owner_id"]),
        kind=str(r["kind"]),
        target=str(r["target"]),
        label=str(r["label"] or ""),
        interval_sec=int(r["interval_sec"]),
        enabled=bool(r["enabled"]),
        include_kw=str(r["include_kw"] or ""),
        exclude_kw=str(r["exclude_kw"] or ""),
        last_check=r["last_check"],
        last_ok=r["last_ok"],
        last_error=str(r["last_error"] or ""),
        cursor=dict(cur or {}),
    )
