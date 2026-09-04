"""Доступ к Postgres.

Ленивая инициализация движка: ``import aegis.platform.db`` не должен требовать живую БД или
даже ``DATABASE_URL`` — иначе падают юнит-тесты, линтеры и ``aegis doctor`` на пустом checkout.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from aegis.platform.config import settings

# Контракт «фабрика сессий»: и `session()` (коммит/rollback), и `async_sessionmaker` подходят.
# Репозитории принимают её в конструктор, чтобы в тестах подставлять fake без реального Postgres.
SessionFactory = Callable[..., AbstractAsyncContextManager[AsyncSession]]

__all__ = [
    "DatabaseUnavailable",
    "SessionFactory",
    "check_connection",
    "get_engine",
    "get_sessionmaker",
    "reset_engine",
    "session",
]


class DatabaseUnavailable(RuntimeError):
    """Двиок не может быть создан (нет настроек/драйвера)."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        cfg = settings()
        _engine = create_async_engine(
            cfg.database_url,
            pool_size=cfg.db_pool_size,
            max_overflow=cfg.db_pool_size,
            pool_pre_ping=True,
            pool_recycle=1800,
            future=True,
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    assert _engine is not None  # narrowing для mypy
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


def reset_engine() -> None:
    """Сбросить кеш движка (тесты, смена DATABASE_URL в dev)."""
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    """Коммит по успеху, rollback по исключению — один путь для всех репозиториев."""
    async with get_sessionmaker()() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


async def check_connection() -> str:
    """Для doctor: версия сервера либо исключение с коротким текстом."""
    from sqlalchemy import text

    try:
        async with session() as s:
            version = await s.scalar(text("SELECT current_setting('server_version')"))
            return str(version)
    except Exception as exc:  # noqa: BLE001 - враппер диагностики, не бизнес-ошибка
        raise DatabaseUnavailable(f"{type(exc).__name__}: {exc}") from exc
