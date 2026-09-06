"""Доступ к Postgres.

Ленивая инициализация движка: ``import aegis.platform.db`` не должен требовать живую БД или
даже ``DATABASE_URL`` — иначе падают юнит-тесты, линтеры и ``aegis doctor`` на пустом checkout.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from sqlalchemy import event
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
        _attach_rls_hook(_engine)
    assert _engine is not None  # narrowing для mypy
    return _engine


_rls_hooked = False


def _attach_rls_hook(engine: AsyncEngine) -> None:
    """Начало транзакции = установка RLS-GUC этого контекста (F2).

    `Session.after_begin`, а не `before_execute`: GUC обязаны жить ровно одну транзакцию
    (``set_config`` с ``is_local=true``), а не «до конца соединения» — иначе переиспользованный
    пул утащит принципала прошлого запроса в чужую транзакцию. Вешается на класс Session (один
    раз на процесс): AsyncSession внутри исполняет ровно этот sync-Session, так что хук ловит
    и raw `session()`, и репозитории с собственной фабрикой. Ошибка хука роняет транзакцию:
    «не удалось связать контекст» не должно тихо означать «связали с дефолтом».
    """
    global _rls_hooked
    if _rls_hooked:
        return
    _rls_hooked = True
    del engine
    from sqlalchemy.engine import Connection
    from sqlalchemy.orm import Session as SyncSession  # noqa: PLC0415

    @event.listens_for(SyncSession, "after_begin")
    def _set_rls_gucs(session: object, transaction: object, connection: Connection) -> None:
        from aegis.platform.rls import effective_scope, guc_statements  # noqa: PLC0415

        del session, transaction
        for stmt in guc_statements(effective_scope()):
            connection.exec_driver_sql(stmt)


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
