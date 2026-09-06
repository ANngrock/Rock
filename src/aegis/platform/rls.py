"""Контекст принципала для Postgres RLS (F2).

Политики Row Level Security читают два GUC — ``app.principal_id`` и ``app.principal_kind``.
Кто их выставляет: этот модуль (contextvar) + хук SQLAlchemy (`after_begin` в ``platform/db.py``),
который делает ``set_config(..., true)`` в НАЧАЛЕ каждой транзакции. Почему хук, а не «пусть
репозитории сами ставят»: GUC, поставленный вручную в одной из сорока сессий, забывается в
сорок первой — и «RLS есть» снова превращается в соглашение, а не свойство. Соглашения
ломаются, хуки — нет.

Безопасность значений: id приводится к int (SQL не строка), kind сверяется с белым списком —
``set_config`` вызывается литералом, который нельзя отравить данными.

Долг по умолчанию (scope не привязан): ``owner`` с id 0 — fail-closed: не «видно всё», а «не
видно ничего полезного». Фоновые демоны (relay, rewrap, backfill, тики) обязаны связывать
``service_scope()`` явно: «daemon» — это привилегия, она должна быть написана в коде, а не
унаследована по умолчанию. Superuser/dev-сессии политик не касаются (RLS их обходит), поэтому
дефолт заметен ровно там, где включено принуждение.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

__all__ = [
    "KINDS",
    "PrincipalScope",
    "current_scope",
    "effective_scope",
    "guc_statements",
    "principal_scope",
    "reset_scope",
    "service_scope",
    "set_scope",
]

#: полный алфавит политик; значение вне списка в миграции невозможно (CHECK), и здесь тоже
KINDS: frozenset[str] = frozenset({"owner", "member", "guest", "service"})


@dataclass(frozen=True, slots=True)
class PrincipalScope:
    """Кто (principal_id), с какими правами (kind) и в каком домохозяйстве (house_id).

    ``house_id`` — владелец ДАННЫХ. Он отдельный, потому что «гость спросил в чате владельца»:
    строки журнала помечены автором (actor), а заметки — владельцем; политика обязана различать
    «чьи данные» и «кто спросил», иначе либо гость видит чужое, либо автор не видит своё.
    """

    principal_id: int
    kind: str
    house_id: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "principal_id", int(self.principal_id))
        object.__setattr__(self, "house_id", int(self.house_id) or int(self.principal_id))
        if self.kind not in KINDS:
            raise ValueError(f"kind {self.kind!r} не в {sorted(KINDS)}")


_UNBOUND = PrincipalScope(0, "owner")
_scope: contextvars.ContextVar[PrincipalScope] = contextvars.ContextVar(
    "aegis.rls.scope", default=_UNBOUND
)


def current_scope() -> PrincipalScope:
    """Текущий связанный контекст (дефолт — невидимый fail-closed)."""
    return _scope.get()


def effective_scope() -> PrincipalScope:
    return _scope.get()


@contextmanager
def principal_scope(principal_id: int, kind: str, house_id: int = 0) -> Iterator[PrincipalScope]:
    """Все транзакции внутри блока несут этого принципала (вложенность восстанавливается).

    Вложенные contextvar-скоупы — норма для asyncio: задача наследует контекст, и фоновый
    ``create_task`` внутри handler'а видит того же принципала, что и ход.
    """
    scope = PrincipalScope(int(principal_id), kind, int(house_id or principal_id))
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


@contextmanager
def service_scope(house_id: int = 0) -> Iterator[PrincipalScope]:
    """Единая точка «я демон, мне видно всё». Шум в логах — если кто-то им воспользовался зря."""
    with principal_scope(0, "service", house_id or 0) as scope:
        yield scope


def set_scope(scope: PrincipalScope) -> contextvars.Token[PrincipalScope]:
    """Явная связка для мест, где scope меняется в середине скоупа (role после resolve)."""
    return _scope.set(scope)


def reset_scope(token: contextvars.Token[PrincipalScope]) -> None:
    _scope.reset(token)


def guc_statements(scope: PrincipalScope | None = None) -> tuple[str, ...]:
    """SQL-трио set_config для начала транзакции. Экранировать нечего: int и enum-белый список."""
    s = scope or _scope.get()
    return (
        f"SELECT set_config('app.principal_id', '{s.principal_id}', true)",
        f"SELECT set_config('app.principal_kind', '{s.kind}', true)",
        f"SELECT set_config('app.house_id', '{s.house_id}', true)",
    )
