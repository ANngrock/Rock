"""Принципалы и гранты (F2): из «владелец и всё» в «несколько субъектов с правами».

Почему не «если owner_id совпал — ок»: предикат ``WHERE owner_id = :me`` в каждом репозитории —
это политика, существующая только там, где о ней не забыли. Ровно одна забытая предиката — и
чужие заметки утекают. Поэтому слой делает три вещи:

* **субъекты как данные** — ``platform.principals`` (owner / member / guest / service) и
  ``platform.principal_grants``: права выдаются и отзываются записью, а не правкой кода;
* **RLS в Postgres как граница** — миграция навешивает политики на каждую таблицу с
  ``owner_id`` (самопроверка миграции падает, если таблицы остались без политики), а приложение
  держит ``WHERE`` как вторую линию, а не единственную;
* **кто спросил ≠ чьи данные** — ``actor_id`` записывается в журнал отдельно от ``owner_id``.
  Пока владелец один, они совпадают; с первого дня, как появятся дети/помощник, расхождение
  обязано быть различимо задним числом, а не «по логу процесса».

Бюджет и kill switch — тоже на принципала (``platform.principal_state``): «приложение
заморожено» и «эта конкретная персона не должна писать» — разные решения, и второе не должно
ломать первое.

Чистое ядро (:func:`allows`, :class:`Principal`) проверяется property-тестами без БД; SQL —
``tests/integration/test_rls.py``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog
from sqlalchemy import text

from aegis.platform.db import SessionFactory, session

__all__ = [
    "Principal",
    "PrincipalGovernance",
    "PrincipalKind",
    "SqlPrincipalStore",
    "Wildcard",
    "allows",
    "parse_roster",
    "permission_for_tool",
]

log = structlog.get_logger(__name__)

PrincipalKind = str  # 'owner' | 'member' | 'guest' | 'service'

#: действия, существующие в системе. Реестр держится здесь, а не в строковых литералах по коду:
#: опечатка в действии должна быть заметна как «нет такого действия», а не как «тихо отказали»
KNOWN_ACTIONS: frozenset[str] = frozenset(
    {
        "memory:write",
        "memory:read",
        "notes:write",
        "notes:read",
        "reminders:write",
        "reminders:read",
        "web:read",
        "tool:pay",
        "journal:read",
        "journal:anchor",
        "export:langfuse",
        "admin:policy",
        "admin:killswitch",
        "admin:principals",
        "data:forget",
    }
)

_OWNER_GRANTS = frozenset({"*"})

_MEMBER_GRANTS = frozenset(
    {
        "memory:read",
        "notes:write",
        "notes:read",
        "reminders:write",
        "reminders:read",
        "web:read",
        "journal:read",
        "journal:anchor",
    }
)

_GUEST_GRANTS = frozenset({"notes:read"})

#: домен инструмента из имени: ``notes.save`` → ``notes``. Реестр, однако, запрещает точки
#: в именах (требование OpenAI), так что для существующих инструментов домен выводится
#: первым сегментом по ``_``; явные имена с нестандартным написанием перечислены ниже.
#: Парность «каждый writes-инструмент имеет грант» сторожит CI (tests/test_principals.py),
#: иначе «новый платёжный инструмент без RBAC» — это дырка, а не опечатка в маппинге.
_TOOL_EXPLICIT: dict[str, str] = {
    "add_note": "notes:write",
    "search_notes": "notes:read",
    "list_notes": "notes:read",
    "save_link": "notes:write",
    "remember_fact": "memory:write",
    "list_facts": "memory:read",
    "forget_fact": "memory:write",
    "web_search": "web:read",
    "fetch_page": "web:read",
    "analyze_image": "web:read",
    "exchange_rate": "web:read",
    "get_datetime": "",
    "anchor_journal": "journal:anchor",
}

_TOOL_DOMAINS = {
    "notes": ("notes:read", "notes:write"),
    "memory": ("memory:read", "memory:write"),
    "reminders": ("reminders:read", "reminders:write"),
    "reminder": ("reminders:read", "reminders:write"),  # реальные имена — set/cancel_reminder
    "web": ("web:read", "web:read"),
    "image": ("web:read", "web:read"),
    "finance": ("memory:read", "tool:pay"),
}


@dataclass(frozen=True, slots=True)
class Wildcard:
    """Маркер «полный доступ» — отдельный тип, чтобы ``"*"`` не встречался в разбросе строк."""

    value: str = "*"


def allows(grants: Iterable[str], action: str) -> bool:
    """Чистое правило: пустое множество грантов не разрешает ничего; ``*`` разрешает всё.

    Отдельная функция — потому что это ЕДИНСТВЕННАЯ точка, где решается «можно ли», и её
    property-тестируют на случайных наборах: «guest без грантов никогда не получает allow» и
    «запрет наследуется при любом наборе разрешений, кроме *.»
    """
    if not action:
        return True
    for grant in grants:
        if grant == "*" or grant == action:
            return True
    return False


def permission_for_tool(tool: str, *, writes: bool) -> str:
    """Какое действие требует вызов инструмента. Пустая строка — «грант не нужен» (время, поиск).

    Порядок: явная карта имён → доменный префикс (``finance_pay`` → ``tool:pay`` при writes).
    Неизвестное writes-имя без гранта здесь — сигнал дописать маппинг, но молча «разрешить
    всё» он не должен превращаться в дырку: тест сверяет реестр целиком.
    """
    if tool in _TOOL_EXPLICIT:
        return _TOOL_EXPLICIT[tool]
    for segment in re.split(r"[._]", tool):
        pair = _TOOL_DOMAINS.get(segment)
        if pair is not None:
            return pair[1] if writes else pair[0]
    return ""


@dataclass(frozen=True, slots=True)
class Principal:
    """Субъект, от имени которого исполняется ход. id — тот же bigint, что и telegram user id."""

    principal_id: int
    kind: PrincipalKind = "guest"
    grants: frozenset[str] = frozenset()
    display_name: str = ""
    status: str = "active"

    @property
    def is_owner(self) -> bool:
        return self.kind == "owner"

    @property
    def usable(self) -> bool:
        return self.status == "active"

    def can(self, action: str) -> bool:
        return self.usable and allows(self.grants, action)

    def journal_actor(self) -> int:
        return int(self.principal_id)


def default_grants(kind: PrincipalKind) -> frozenset[str]:
    if kind == "owner":
        return _OWNER_GRANTS
    if kind == "member":
        return _MEMBER_GRANTS
    if kind == "service":
        return _OWNER_GRANTS
    return _GUEST_GRANTS


def parse_roster(spec: str) -> dict[int, PrincipalKind]:
    """``"42:member,77:guest"`` → ``{42: 'member', 77: 'guest'}``. Мусорные пары игнорируются.

    Молчаливый пропуск — осознанный: конфигурация не должна уметь ронять бот строкой в env;
    doctor показывает разобранный состав, и расхождение видно там.
    """
    out: dict[int, PrincipalKind] = {}
    for chunk in (spec or "").split(","):
        head, _, tail = chunk.strip().partition(":")
        if not head or tail not in ("owner", "member", "guest", "service"):
            continue
        try:
            out[int(head)] = tail
        except ValueError:
            continue
    return out


class PrincipalGovernance(Protocol):
    """Бюджет и kill switch на принципала (не на приложение)."""

    async def kill_switch(self, principal_id: int) -> dict[str, Any] | None: ...

    async def set_kill_switch(self, principal_id: int, active: bool, reason: str) -> None: ...

    async def daily_budget(self, principal_id: int) -> float | None: ...

    async def set_daily_budget(self, principal_id: int, limit_usd: float | None) -> None: ...

    async def spent_today(self, principal_id: int) -> float: ...


class NullPrincipalGovernance:
    """Без БД права — только в конфиге; «приостановлено» и «бюджет» живут в процессе-единстве."""

    def __init__(self) -> None:
        self._kill: dict[int, dict[str, Any]] = {}
        self._budget: dict[int, float] = {}

    async def kill_switch(self, principal_id: int) -> dict[str, Any] | None:
        return self._kill.get(int(principal_id))

    async def set_kill_switch(self, principal_id: int, active: bool, reason: str) -> None:
        pid = int(principal_id)
        if active:
            self._kill[pid] = {"active": True, "reason": reason[:500]}
        else:
            self._kill.pop(pid, None)

    async def daily_budget(self, principal_id: int) -> float | None:
        return self._budget.get(int(principal_id))

    async def set_daily_budget(self, principal_id: int, limit_usd: float | None) -> None:
        pid = int(principal_id)
        if limit_usd is None:
            self._budget.pop(pid, None)
        else:
            self._budget[pid] = float(limit_usd)

    async def spent_today(self, principal_id: int) -> float:
        return 0.0


class SqlPrincipalStore:
    """Магазин принципалов + грантов + состояния. Один класс на три таблицы — намеренно:
    они читаются только вместе (principal без грантов и без лимитов — не субъект, а запись).
    """

    def __init__(
        self, cfg: Any | None = None, *, session_factory: SessionFactory | None = None
    ) -> None:
        self._sm = session_factory
        self._cfg = cfg

    def _session(self) -> Any:
        return self._sm() if self._sm is not None else session()

    async def ensure(self, principal_id: int, *, kind: PrincipalKind, name: str = "") -> None:
        """Зарегистрировать, если нет. Никакого «обновить kind»: смена типа — административное
        решение (:meth:`set_kind`), а не побочный эффект первого сообщения."""
        async with self._session() as s:
            await s.execute(
                text(
                    "INSERT INTO platform.principals (principal_id, subject, kind, display_name)"
                    " VALUES (:id, :subject, :kind, :name)"
                    " ON CONFLICT (principal_id) DO NOTHING"
                ).bindparams(
                    id=int(principal_id),
                    subject=f"telegram:{int(principal_id)}",
                    kind=kind,
                    # NOT NULL по схеме: «безымянный» принципал — не NULL, а id: ленивый
                    # ensure для незнакомца не имеет права падать на вставке
                    name=(name or str(int(principal_id)))[:120],
                )
            )
            for action in sorted(default_grants(kind)):
                await s.execute(
                    text(
                        "INSERT INTO platform.principal_grants (principal_id, action, granted_by)"
                        " VALUES (:id, :action, :id) ON CONFLICT DO NOTHING"
                    ).bindparams(id=int(principal_id), action=action)
                )
            await s.commit()

    async def set_kind(self, principal_id: int, kind: PrincipalKind) -> None:
        async with self._session() as s:
            await s.execute(
                text(
                    "UPDATE platform.principals SET kind = :kind, updated_at = now()"
                    " WHERE principal_id = :id"
                ),
                {"id": int(principal_id), "kind": kind},
            )
            await s.execute(
                text("DELETE FROM platform.principal_grants WHERE principal_id = :id"),
                {"id": int(principal_id)},
            )
            for action in sorted(default_grants(kind)):
                await s.execute(
                    text(
                        "INSERT INTO platform.principal_grants (principal_id, action, granted_by)"
                        " VALUES (:id, :action, :id) ON CONFLICT DO NOTHING"
                    ),
                    {"id": int(principal_id), "action": action},
                )
            await s.commit()

    async def grant(self, principal_id: int, action: str, *, by: int) -> None:
        if action not in KNOWN_ACTIONS and action != "*":
            raise ValueError(
                f"неизвестное действие {action!r}: сначала заведите его в KNOWN_ACTIONS"
            )
        async with self._session() as s:
            await s.execute(
                text(
                    "INSERT INTO platform.principal_grants (principal_id, action, granted_by)"
                    " VALUES (:id, :action, :by) ON CONFLICT DO NOTHING"
                ),
                {"id": int(principal_id), "action": action, "by": int(by)},
            )
            await s.commit()

    async def revoke(self, principal_id: int, action: str) -> None:
        async with self._session() as s:
            await s.execute(
                text(
                    "DELETE FROM platform.principal_grants"
                    " WHERE principal_id = :id AND action = :action"
                ),
                {"id": int(principal_id), "action": action},
            )
            await s.commit()

    async def load(self, principal_id: int) -> Principal | None:
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "SELECT p.kind, p.status, coalesce(p.display_name, '') AS name,"
                            " coalesce(array_agg(g.action) FILTER (WHERE g.action IS NOT NULL), "
                            " ARRAY[]::text[]) AS grants"
                            " FROM platform.principals p"
                            " LEFT JOIN platform.principal_grants g"
                            "   ON g.principal_id = p.principal_id"
                            " WHERE p.principal_id = :id GROUP BY 1, 2, 3"
                        ).bindparams(id=int(principal_id))
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return Principal(
            principal_id=int(principal_id),
            kind=str(row["kind"]),
            grants=frozenset(str(g) for g in row["grants"]),
            display_name=str(row["name"]),
            status=str(row["status"]),
        )

    async def list_principals(self) -> list[dict[str, Any]]:
        async with self._session() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            "SELECT p.principal_id, p.subject, p.kind, p.status, p.display_name,"
                            " coalesce(array_agg(g.action) FILTER (WHERE g.action IS NOT NULL),"
                            " ARRAY[]::text[]) AS grants,"
                            " st.daily_budget_usd, st.kill_switch"
                            " FROM platform.principals p"
                            " LEFT JOIN platform.principal_grants g"
                            "   ON g.principal_id = p.principal_id"
                            " LEFT JOIN platform.principal_state st"
                            "   ON st.principal_id = p.principal_id"
                            " GROUP BY 1, 2, 3, 4, 5, st.daily_budget_usd, st.kill_switch"
                            " ORDER BY p.principal_id"
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    # --- governance: бюджет/kill switch на принципала ---

    async def _state(self, s: Any, principal_id: int) -> dict[str, Any]:
        row = (
            (
                await s.execute(
                    text(
                        "SELECT daily_budget_usd, kill_switch FROM platform.principal_state"
                        " WHERE principal_id = :id"
                    ).bindparams(id=int(principal_id))
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else {}

    async def kill_switch(self, principal_id: int) -> dict[str, Any] | None:
        async with self._session() as s:
            state = await self._state(s, principal_id)
        raw = state.get("kill_switch")
        if not raw:
            return None
        return dict(raw) if isinstance(raw, Mapping) else None

    async def set_kill_switch(self, principal_id: int, active: bool, reason: str) -> None:
        # jsonb собирается параметрами (jsonb_set/to_jsonb), а не конкатенацией строк: причина
        # приходит от пользователя, и кавычки в ней — не повод ломать миграцию данных
        async with self._session() as s:
            if active:
                await s.execute(
                    text(
                        "INSERT INTO platform.principal_state"
                        " (principal_id, kill_switch, updated_at)"
                        " VALUES (:id, CAST(:payload AS jsonb), now())"
                        " ON CONFLICT (principal_id) DO UPDATE"
                        " SET kill_switch = EXCLUDED.kill_switch, updated_at = now()"
                    ).bindparams(id=int(principal_id), payload='{"active": true}'),
                )
                await s.execute(
                    text(
                        "UPDATE platform.principal_state"
                        " SET kill_switch = jsonb_set(coalesce(kill_switch, '{}'::jsonb),"
                        " '{reason}', to_jsonb(CAST(:reason AS text)), true)"
                        " WHERE principal_id = :id"
                    ).bindparams(id=int(principal_id), reason=reason[:500]),
                )
            else:
                # НЕ NULL: колонка NOT NULL по схеме, а в строке живёт ещё и бюджет — удаление
                # строки стёрло бы лимит вместе с паузой. Сброс = дефолт-объект без reason
                await s.execute(
                    text(
                        "UPDATE platform.principal_state"
                        " SET kill_switch = '{\"active\": false}'::jsonb, updated_at = now()"
                        " WHERE principal_id = :id"
                    ).bindparams(id=int(principal_id)),
                )
            await s.commit()

    async def daily_budget(self, principal_id: int) -> float | None:
        async with self._session() as s:
            state = await self._state(s, principal_id)
        value = state.get("daily_budget_usd")
        return None if value is None else float(value)

    async def set_daily_budget(self, principal_id: int, limit_usd: float | None) -> None:
        async with self._session() as s:
            await s.execute(
                text(
                    "INSERT INTO platform.principal_state"
                    " (principal_id, daily_budget_usd, updated_at)"
                    " VALUES (:id, CAST(:limit AS numeric), now())"
                    " ON CONFLICT (principal_id) DO UPDATE"
                    " SET daily_budget_usd = EXCLUDED.daily_budget_usd, updated_at = now()"
                ).bindparams(
                    id=int(principal_id),
                    limit=None if limit_usd is None else f"{float(limit_usd):.6f}",
                )
            )
            await s.commit()

    async def spent_today(self, principal_id: int) -> float:
        """Потрачено сегодня по журналу (не по KV): «сколько ушло на детей» считается тем же
        источником истины, что и всё остальное."""
        sql = (
            "SELECT coalesce(sum(cost_usd), 0) FROM governance.decision_records"
            " WHERE (owner_id = :id OR actor_id = :id) AND created_at::date = CURRENT_DATE"
            "   AND kind IN ('llm_call', 'turn_summary')"
        )
        async with self._session() as s:
            return float(await s.scalar(text(sql).bindparams(id=int(principal_id))) or 0.0)


async def resolve_principal(
    store: SqlPrincipalStore | None,
    actor_id: int,
    *,
    owner_id: int,
    roster: Mapping[int, PrincipalKind] | None = None,
) -> Principal:
    """Кто этот telegram-id сегодня. Порядок: владелец → реестр → неизвестный (guest без грантов).

    Неизвестный — guest, а не отказ: право «не видеть чужое» должно быть свойством схемы,
    а не наличием записи. Запись при этом создаётся лениво, чтобы ``aegis principals list``
    показывал всех, кто когда-либо постучался.
    """
    actor = int(actor_id)
    owner = int(owner_id)
    if actor == owner:
        if store is not None:
            known = await store.load(actor)
            if known is None:
                await store.ensure(actor, kind="owner")
                known = await store.load(actor)
            if known is not None and known.kind == "owner":
                return known
        return Principal(
            principal_id=actor, kind="owner", grants=default_grants("owner"), display_name="owner"
        )
    kind = (roster or {}).get(actor, "guest")
    if store is not None:
        known = await store.load(actor)
        if known is None:
            await store.ensure(actor, kind=kind)
            return Principal(
                principal_id=actor, kind=kind, grants=default_grants(kind), display_name=str(actor)
            )
        # гость не может «вырасти» до владельца молча: сверяем kind с конфигурацией при каждом
        # прочтении, и расхождение видно (kind из реестра, гранты — из выданных записей)
        if known.kind != kind:
            grants = known.grants if kind == "member" else default_grants(kind)
            return Principal(known.principal_id, kind, grants, known.display_name, known.status)
        return known
    return Principal(principal_id=actor, kind=kind, grants=default_grants(kind))


@dataclass(slots=True)
class PrincipalSnapshot:
    """Снимок прав для policy-оценки одного хода (не держать запрос на каждый tool_call)."""

    principal: Principal
    budget_limit: float | None = None
    budget_spent: float = 0.0
    killed: dict[str, Any] = field(default_factory=dict)

    @property
    def kill_active(self) -> bool:
        return bool(self.killed.get("active"))

    @property
    def budget_exhausted(self) -> bool:
        return self.budget_limit is not None and self.budget_spent >= self.budget_limit

    def ratio(self) -> float:
        if not self.budget_limit:
            return 0.0
        return min(self.budget_spent / self.budget_limit, 99.0)


async def snapshot_for(
    governance: PrincipalGovernance | None, principal: Principal, *, fallback_limit: float | None
) -> PrincipalSnapshot:
    if governance is None:
        return PrincipalSnapshot(principal=principal, budget_limit=fallback_limit)
    limit = await governance.daily_budget(principal.principal_id)
    spent = 0.0
    killed: dict[str, Any] = {}
    try:
        killed_state = await governance.kill_switch(principal.principal_id)
        if killed_state:
            killed = dict(killed_state)
        spent = await governance.spent_today(principal.principal_id)
    except Exception as exc:  # noqa: BLE001 — «не удалось прочитать лимит» не отказ в работе
        log.warning("principals.state_failed", err=repr(exc)[:200])
    return PrincipalSnapshot(
        principal=principal,
        budget_limit=limit if limit is not None else fallback_limit,
        budget_spent=spent,
        killed=killed,
    )


def audit_actor_line(actors: Sequence[int]) -> str:
    """Строка «для кого из нас» — одна строка на весь журнал хода."""
    return ", ".join(str(int(a)) for a in actors)
