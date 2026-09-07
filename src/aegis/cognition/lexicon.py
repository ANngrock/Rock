"""Лексикон владельца: жаргон, сокращения, имена — объяснённые один раз, объясняются всегда.

«Го кинь выгрузку» — модель угадает, угадает, ещё раз угадает. Одна запись «го = давай/сейчас»
превращает догадку в факт. Отсюда правило хранения: term нормализуется (lower, схлопнутые
пробелы), иначе «ГО» и «го» — два слова для БД и одно для человека.

kind разделён CHECK'ом: term (что значит), person (кто это — «Тимур = подрядчик по электрике»),
style (устойчивая просьба о тоне — «в письмах мне на «Вы»»). Разные kinds попадают в разные
места <cognition>-блока: человеку важно, что «Костя» — это свёкор, а не пользователь.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

__all__ = ["LexEntry", "SqlLexicon", "hits_in", "normalize_term"]

_KNDS = ("term", "person", "style")
_WS = re.compile(r"\s+")


def normalize_term(term: str) -> str:
    t = _WS.sub(" ", (term or "").strip()).lower()
    if not 1 <= len(t) <= 64:  # noqa: PLR2004 — как в CHECK миграции; длиннее — это фраза, не термин
        raise ValueError("термин — от 1 до 64 символов")
    return t


@dataclass(frozen=True, slots=True)
class LexEntry:
    term: str
    means: str
    kind: str


def hits_in(entries: list[LexEntry], msg: str) -> list[LexEntry]:
    """Какие термины встречаются в тексте. Подстрочник, а не замена: оригинал виден целиком."""
    low = msg.lower()
    # длина>=2: однобуквенный «термин» матчил бы всё подряд
    return [e for e in entries if len(e.term) >= 2 and e.term in low]


def prompt_block(entries: list[LexEntry]) -> str:
    """Словарная вставка в <cognition>. Пусто — пусто, не «(словарь не найден)»."""
    if not entries:
        return ""
    lines = {"term": "термины", "person": "люди", "style": "пожелания к тону"}
    out: list[str] = []
    for kind in _KNDS:
        got = [e for e in entries if e.kind == kind]
        if got:
            out.append(lines[kind] + ": " + "; ".join(f"{e.term} = {e.means}" for e in got))
    return "\n".join(out)


class SqlLexicon:
    def __init__(self, *, session_factory: Any = None) -> None:
        self._sm = session_factory

    def _session(self) -> Any:
        from aegis.platform.db import session

        return self._sm() if self._sm is not None else session()

    async def upsert(self, owner_id: int, term: str, means: str, kind: str = "term") -> str:
        t, m = normalize_term(term), " ".join((means or "").split())
        if kind not in _KNDS:
            raise ValueError(f"kind — одно из {_KNDS}")
        if not 1 <= len(m) <= 500:  # noqa: PLR2004 — как CHECK миграции
            raise ValueError("значение — от 1 до 500 символов")
        async with self._session() as s:
            await s.execute(
                text(
                    "INSERT INTO cognition.lexicon (owner_id, term, means, kind)"
                    " VALUES (:o, :t, :m, :k)"
                    " ON CONFLICT (owner_id, term) DO UPDATE SET means = EXCLUDED.means,"
                    " kind = EXCLUDED.kind, updated_at = now()"
                ),
                {"o": int(owner_id), "t": t, "m": m, "k": kind},
            )
            await s.commit()
        return t

    async def remove(self, owner_id: int, term: str) -> bool:
        async with self._session() as s:
            updated = (
                await s.execute(
                    text("DELETE FROM cognition.lexicon WHERE owner_id = :o AND term = :t"),
                    {"o": int(owner_id), "t": normalize_term(term)},
                )
            ).rowcount
            await s.commit()
        return bool(updated)

    async def list_terms(self, owner_id: int, *, kind: str | None = None) -> list[LexEntry]:
        sql = "SELECT term, means, kind FROM cognition.lexicon WHERE owner_id = :o"
        args: dict[str, Any] = {"o": int(owner_id)}
        if kind is not None:
            sql += " AND kind = :k"
            args["k"] = kind
        sql += " ORDER BY term"
        async with self._session() as s:
            rows = (await s.execute(text(sql), args)).mappings().all()
        return [
            LexEntry(term=str(r["term"]), means=str(r["means"]), kind=str(r["kind"])) for r in rows
        ]
