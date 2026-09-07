"""Журнал голоса: что услышали, что сказали. Одна строка на попытку, включая провал.

Зачем провалы в той же таблице, что и успехи: «бот проигнорировал мое голосовое» без строки в БД
неразрешимо — была ли тишина отказом движка или потерянным сообщением. С ошибкой в voice_log
ответ один: `SELECT * FROM cognition.voice_log WHERE ok=false ORDER BY created_at DESC LIMIT 5`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

__all__ = ["log_voice"]


async def log_voice(
    owner_id: int,
    *,
    direction: str,
    engine: str,
    transcription: str | None = None,
    seconds: int | None = None,
    ok: bool = True,
    error: str | None = None,
) -> None:
    if direction not in ("in", "out"):
        raise ValueError("direction: in|out")
    from aegis.platform.db import session

    async with session() as s:
        await s.execute(
            text(
                "INSERT INTO cognition.voice_log"
                " (owner_id, direction, engine, text, seconds, ok, error)"
                " VALUES (:o, :d, :e, :t, :s, :k, :r)"
            ),
            {
                "o": int(owner_id),
                "d": direction,
                "e": engine[:64],
                "t": (transcription or "")[:8000] or None,
                "s": seconds,
                "k": bool(ok),
                "r": (error or "")[:500] or None,
            },
        )
        await s.commit()


def recent_failed(rows: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    """Для doctor: последние провалы — первыми."""
    bad = [r for r in rows if not r.get("ok")]
    return bad[:limit]
