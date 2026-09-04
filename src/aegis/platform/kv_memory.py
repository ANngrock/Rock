"""In-memory реализация порта KV — для демо, тестов и запуска без Redis.

Зачем это нужно: Redis — единственный сервис, без которого бот в шаге 1 вообще не
поднимается (история диалога, pending-подтверждения, счётчик дневного бюджета). Для
первого запуска «пощупать» и для CI поднимать Redis избыточно, а требовать его — значит
смешивать «проверить, что агент думает» и «проверить, что инфраструктура жива».

Чем это НЕ является: состояние живёт только в этом процессе. Рестарт = сброс истории,
обнуление дневного бюджета, потеря pending. В проде `AEGIS_KV_BACKEND=memory` — выстрел
в собственную ногу: два процесса (бот + будущий воркер Temporal) увидят разные миры.
"""

from __future__ import annotations

import time
from typing import Any

__all__ = ["MemoryKV"]


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray | memoryview):
        return bytes(value)
    if isinstance(value, bool):
        return b"1" if value else b"0"
    if isinstance(value, int | float):
        return str(value).encode()
    return str(value).encode()


class MemoryKV:
    """Совместим с тем, что использует aegis: get/set/delete/getdel/incrbyfloat/expire.

    Значения храним байтами — как redis с ``decode_responses=False``, чтобы код в
    supervisor/audit не раздваивался на «тут строка, там bytes».
    """

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}
        self._expires_at: dict[str, float] = {}

    # ---------------------------------------------------------------- internals

    def _expired(self, name: str) -> bool:
        deadline = self._expires_at.get(name)
        if deadline is None:
            return False
        if deadline > time.monotonic():
            return False
        self._data.pop(name, None)
        self._expires_at.pop(name, None)
        return True

    # ------------------------------------------------------------------- KV API

    async def get(self, name: str) -> bytes | None:
        if self._expired(name):
            return None
        return self._data.get(name)

    async def set(self, name: str, value: Any, *, ex: int | None = None) -> bool:
        self._data[name] = _as_bytes(value)
        if ex is not None:
            self._expires_at[name] = time.monotonic() + ex
        else:
            self._expires_at.pop(name, None)
        return True

    async def delete(self, *names: str) -> int:
        # как redis: в счётчик идут только реально удалённые ключи
        removed = 0
        for name in names:
            existed = name in self._data and not self._expired(name)
            self._data.pop(name, None)
            self._expires_at.pop(name, None)
            removed += 1 if existed else 0
        return removed

    async def getdel(self, name: str) -> bytes | None:
        value = await self.get(name)
        self._data.pop(name, None)
        self._expires_at.pop(name, None)
        return value

    async def incrbyfloat(self, name: str, amount: float = 1.0) -> float:
        current = 0.0
        raw = await self.get(name)
        if raw is not None:
            try:
                current = float(raw)
            except ValueError:  # мусор в ключем-значении: считаем с нуля, как redis упал бы
                current = 0.0
        nxt = current + amount
        self._data[name] = _as_bytes(nxt)
        return nxt

    async def expire(self, name: str, seconds: int) -> bool:
        if self._expired(name) or name not in self._data:
            return False
        self._expires_at[name] = time.monotonic() + seconds
        return True

    # --------------------------------------------------------------------- misc

    async def aclose(self) -> None:
        self._data.clear()
        self._expires_at.clear()

    def keys(self) -> list[str]:
        """Отладка/тесты: что сейчас лежит (истёкшие ключи уже отсяются)."""
        return sorted(k for k in list(self._data) if not self._expired(k))

    def ttl(self, name: str) -> float | None:
        deadline = self._expires_at.get(name)
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())
