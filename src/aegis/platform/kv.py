"""Узкий порт KV-хранилища (Redis) и ключи состояния процесса.

Здесь живут только «летучие» вещи: история диалога, ожидающие подтверждения, счётчик бюджета,
kill switch. Всё, что должно пережить инцидент, — в Postgres (принцип 1).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

__all__ = ["KV", "SealedKV", "history_key", "killswitch_key", "pending_key"]


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


class SealedKV:
    """Обёртка redis-клиента: значения «личных» ключей уходят в Redis запечатанными.

    Фронт выбран по риску, а не по ширине: история диалога и pending (память о разговоре
    переживает рестарты в AOF Redis) — личные; счётчик бюджета — числа, их шифрование ломает
    INCRBYFLOAT ради нуля пользы. Ключи вне списка проходят насквозь, незапечатанные значения
    читаются как есть: включение ротации задним числом не требует «переливки» всего хранилища.
    """

    _SEAL_PREFIXES = ("hist:", "pending:")

    def __init__(self, inner: KV, provider: Callable[[], Any]) -> None:
        self._inner = inner
        #: () -> BlobCipher | None; vault процесса, ротация переставляет ключи под нами
        self._provider = provider

    @staticmethod
    def _seals(name: str) -> bool:
        return str(name).startswith(SealedKV._SEAL_PREFIXES)

    async def set(self, name: str, value: Any, *, ex: int | None = None) -> Any:
        if self._seals(name):
            from aegis.platform.vault import seal_text  # noqa: PLC0415 — опциональный фронт

            cipher = self._provider()
            text = (
                value.decode("utf-8", "replace")
                if isinstance(value, bytes | bytearray)
                else str(value)
            )
            sealed = seal_text(cipher, text)
            if cipher is not None and sealed != text:
                return await self._inner.set(name, sealed.encode("utf-8"), ex=ex)
        return await self._inner.set(name, value, ex=ex)

    async def get(self, name: str) -> bytes | str | None:
        raw = await self._inner.get(name)
        return await self._unseal(raw)

    async def getdel(self, name: str) -> bytes | str | None:
        raw = await self._inner.getdel(name)
        return await self._unseal(raw)

    async def _unseal(self, raw: bytes | str | None) -> bytes | str | None:
        if raw is None:
            return raw
        blob = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode("utf-8")
        if not blob.startswith(b"aeg1s:"):
            return raw
        from aegis.platform.vault import open_text  # noqa: PLC0415

        text = open_text(self._provider(), blob.decode("utf-8"))
        return text.encode("utf-8") if isinstance(raw, (bytes, bytearray)) else text

    def __getattr__(
        self, item: str
    ) -> Any:  # делег без белого списка: delete/incrbyfloat/expire/ping…
        return getattr(self._inner, item)
