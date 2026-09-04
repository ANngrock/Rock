"""Узкий порт KV-хранилища (Redis) и ключи состояния процесса.

Здесь живут только «летучие» вещи: история диалога, ожидающие подтверждения, счётчик бюджета,
kill switch. Всё, что должно пережить инцидент, — в Postgres (принцип 1).
"""

from __future__ import annotations

from typing import Any, Protocol

__all__ = ["KV", "history_key", "killswitch_key", "pending_key"]


class KV(Protocol):
    """Узкий порт под redis-py: имена и сигнатуры совпадают с AsyncRedis, чтобы реальный клиент
    подходил структурно, а мокам в тестах не требовался лишний API."""

    async def get(self, name: str) -> bytes | str | None: ...

    async def set(self, name: str, value: Any, *, ex: int | None = None) -> Any: ...

    async def delete(self, *names: str) -> int: ...

    async def getdel(self, name: str) -> bytes | str | None: ...

    async def incrbyfloat(self, name: str, amount: float = 1.0) -> float: ...

    async def expire(self, name: str, seconds: int) -> bool | int: ...


def history_key(owner_id: int) -> str:
    return f"hist:{owner_id}"


def pending_key(pending_id: str) -> str:
    return f"pending:{pending_id}"


def killswitch_key() -> str:
    return "governance:killswitch"
