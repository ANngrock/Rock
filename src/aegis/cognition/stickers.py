"""Стикеры: библиотека file_id на household + детерминированный выбор по настроению.

Telegram-бот не «придумывает» стикер — он умеет только отправить file_id, который кто-то
заготовил. Значит «реакция наклейкой» честно делается так: владелец один раз подписывает
коллекцию (имя → mood-теги), дальше бот выбирает из подписанного. Никакого «сгенерирую стикер»
— это было бы обещанием, которое Telegram не выполнит.

Выбор детерминирован по seed (обычно message_id): один и тот же ход в воспроизведении (принцип 4)
даёт ту же наклейку, а не лотерею по времени запроса.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

__all__ = ["SqlStickers", "Sticker", "pick_sticker"]


@dataclass(frozen=True, slots=True)
class Sticker:
    name: str
    file_id: str
    moods: tuple[str, ...] = field(default=())


def pick_sticker(stickers: list[Sticker], mood: str, *, seed: str) -> Sticker | None:
    """Стикеры с тегом настроения; нет подходящих — пустые теги («на все случаи»); ни того ни
    другого — None (отсутствие реакции лучше случайной)."""
    if not stickers:
        return None
    pool = [s for s in stickers if mood in s.moods] or [s for s in stickers if not s.moods]
    if not pool:
        return None
    idx = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) % len(pool)
    return pool[idx]


_SELECT = "SELECT name, file_id, moods FROM cognition.stickers WHERE owner_id = :o AND enabled"


class SqlStickers:
    def __init__(self, *, session_factory: Any = None) -> None:
        self._sm = session_factory

    def _session(self) -> Any:
        from aegis.platform.db import session

        return self._sm() if self._sm is not None else session()

    async def add(self, owner_id: int, name: str, file_id: str, moods: list[str]) -> None:
        n, f = (name or "").strip()[:40], (file_id or "").strip()
        if not 1 <= len(n) <= 40 or len(f) < 20:  # noqa: PLR2004 — короткий file_id — не file_id
            raise ValueError("имя 1..40 символов; file_id — длинная строка из Telegram")
        clean = sorted({m.strip().lower() for m in moods if m.strip()})[:10]
        async with self._session() as s:
            await s.execute(
                text(
                    "INSERT INTO cognition.stickers (owner_id, name, file_id, moods)"
                    " VALUES (:o, :n, :f, :m)"
                    " ON CONFLICT (owner_id, name) DO UPDATE SET file_id = EXCLUDED.file_id,"
                    " moods = EXCLUDED.moods"
                ),
                {"o": int(owner_id), "n": n, "f": f, "m": clean},
            )
            await s.commit()

    async def remove(self, owner_id: int, name: str) -> bool:
        async with self._session() as s:
            updated = (
                await s.execute(
                    text(
                        "DELETE FROM cognition.stickers"
                        " WHERE owner_id = :o AND lower(name) = lower(:n)"
                    ),
                    {"o": int(owner_id), "n": (name or "").strip()[:40]},
                )
            ).rowcount
            await s.commit()
        return bool(updated)

    async def list_stickers(self, owner_id: int) -> list[Sticker]:
        async with self._session() as s:
            rows = (
                (await s.execute(text(_SELECT + " ORDER BY name"), {"o": int(owner_id)}))
                .mappings()
                .all()
            )
        return [
            Sticker(name=str(r["name"]), file_id=str(r["file_id"]), moods=tuple(r["moods"] or ()))
            for r in rows
        ]
