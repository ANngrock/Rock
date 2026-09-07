"""Хранилище хаба автоматизации: эндпоинты, журнал runs, входящие вебхуки.

Секреты — только запечатанными: колонка ``secrets`` хранит JSON, завернутый
:func:`aegis.platform.vault.seal_text`; без ключарки честно отказываемся хранить секреты
в открытом виде (лучше «нет секретов», чем «секреты утекли с бэкапом»). Имя секрета видно
в ``/menu`` и в списке — значение нет никогда.
"""

from __future__ import annotations

import json
import re
import secrets as _secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from aegis.platform.db import session

__all__ = ["EndpointRow", "HookRow", "SqlAutomationStore"]

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,59}$")
_HOOK_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,40}$")
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_SECRET_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,39}$")

_EP_COLS = (
    "id, owner_id, name, method, url, headers, body_template, secrets,"
    " timeout_ms, enabled, last_run, last_status"
)


@dataclass(slots=True)
class EndpointRow:
    id: str
    owner_id: int
    name: str
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body_template: str = ""
    secret_names: tuple[str, ...] = ()
    timeout_ms: int = 15000
    enabled: bool = True
    last_run: datetime | None = None
    last_status: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "method": self.method,
            "url": self.url,
            "headers": self.headers,
            "body_template": self.body_template,
            "secrets": list(self.secret_names),
            "timeout_ms": self.timeout_ms,
            "enabled": self.enabled,
            "last_status": self.last_status,
            "last_run": self.last_run.astimezone(UTC).isoformat(timespec="minutes")
            if self.last_run
            else None,
        }


@dataclass(slots=True)
class HookRow:
    id: str
    owner_id: int
    name: str
    policy: str
    rate_per_min: int
    enabled: bool
    fires: int
    last_fire: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "policy": self.policy,
            "rate_per_min": self.rate_per_min,
            "enabled": self.enabled,
            "fires": self.fires,
            "last_fire": self.last_fire.astimezone(UTC).isoformat(timespec="minutes")
            if self.last_fire
            else None,
        }


def _cipher() -> Any:
    from aegis.platform.config import settings  # noqa: PLC0415
    from aegis.platform.vault import process_cipher  # noqa: PLC0415

    return process_cipher(settings())


def _seal(plain: str) -> str:
    from aegis.platform.vault import seal_text  # noqa: PLC0415

    return seal_text(_cipher(), plain)


def _open(stored: str) -> str:
    from aegis.platform.vault import open_text  # noqa: PLC0415

    return open_text(_cipher(), stored)


def _ep_row(r: Any, *, include_secrets: bool = False) -> tuple[EndpointRow, dict[str, str]]:
    secrets_map: dict[str, str] = {}
    raw_secrets = str(r["secrets"] or "")
    if raw_secrets:
        try:
            secrets_map = json.loads(_open(raw_secrets))
        except (ValueError, RuntimeError) as exc:
            raise RuntimeError(f"секреты «{r['name']}» не читаются: {exc}") from exc
    try:
        headers = json.loads(str(r["headers"] or "{}"))
    except ValueError:
        headers = {}
    row = EndpointRow(
        id=str(r["id"]),
        owner_id=int(r["owner_id"]),
        name=str(r["name"]),
        method=str(r["method"]),
        url=str(r["url"]),
        headers={str(k): str(v) for k, v in (headers or {}).items()},
        body_template=str(r["body_template"] or ""),
        secret_names=tuple(sorted(secrets_map)),
        timeout_ms=int(r["timeout_ms"]),
        enabled=bool(r["enabled"]),
        last_run=r["last_run"],
        last_status=str(r["last_status"] or ""),
    )
    return row, (secrets_map if include_secrets else {})


class SqlAutomationStore:
    def __init__(self, *, session_factory: Any = None) -> None:
        self._sm = session_factory

    def _session(self) -> Any:
        return self._sm() if self._sm is not None else session()

    # ---------- исходящие эндпоинты ----------

    async def upsert_endpoint(
        self,
        *,
        owner_id: int,
        name: str,
        url: str,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        body_template: str = "",
        secrets_map: dict[str, str] | None = None,
        timeout_ms: int | None = None,
        enabled: bool = True,
    ) -> EndpointRow:
        name = name.strip().lower()
        if not _NAME_RE.match(name):
            raise ValueError("имя: строчные буквы/цифры/-/_, 2..60")
        url = url.strip()
        if not url.startswith(("http://", "https://")) or any(c in url for c in " \t\r\n"):
            raise ValueError("url — http(s) без пробелов")
        method = method.strip().upper()
        if method not in _METHODS:
            raise ValueError(f"method: {'/'.join(_METHODS)}")
        hdrs = {str(k).strip()[:80]: str(v).strip()[:500] for k, v in (headers or {}).items()}
        smap: dict[str, str] = {}
        for k, v in (secrets_map or {}).items():
            key = str(k).strip().upper()
            if not _SECRET_KEY_RE.match(key):
                raise ValueError(f"имя секрета {key!r} не подходит (латиница ВЕРХНИЙ_РЕГИСТР)")
            smap[key] = str(v)[:2000]
        if smap and _cipher() is None:
            raise ValueError(
                "ключарки нет (AEGIS_MASTER_KEY) — секреты хранить нечем; включите шифрование"
                " либо уберите секреты (тогда заголовки — только открытые)"
            )
        timeout_ms = max(500, min(120000, int(timeout_ms or 15000)))
        sql = (  # интерполяция только модульной константой
            "INSERT INTO automation.endpoint (owner_id, name, method, url, headers,"  # noqa: S608
            " body_template, secrets, timeout_ms, enabled) VALUES (:o, :n, :m, :u, :h, :b,"
            " COALESCE(:sec, ''), :tm, :en) ON CONFLICT (owner_id, name) DO UPDATE SET"
            " method = :m, url = :u, headers = :h, body_template = :b, secrets ="
            " COALESCE(:sec, endpoint.secrets), timeout_ms = :tm, enabled = :en,"
            f" updated_at = now() RETURNING {_EP_COLS}"
        )
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(sql),
                        {
                            "o": int(owner_id),
                            "n": name,
                            "m": method,
                            "u": url,
                            "h": json.dumps(hdrs, ensure_ascii=False),
                            "b": body_template[:4000],
                            "sec": _seal(json.dumps(smap, ensure_ascii=False)) if smap else None,
                            "tm": timeout_ms,
                            "en": bool(enabled),
                        },
                    )
                )
                .mappings()
                .one()
            )
            await s.commit()
        row, _ = _ep_row(row)
        return row

    async def list_endpoints(self, *, owner_id: int) -> list[EndpointRow]:
        async with self._session() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            f"SELECT {_EP_COLS} FROM automation.endpoint WHERE owner_id = :o"  # noqa: S608
                            " ORDER BY name"
                        ),
                        {"o": int(owner_id)},
                    )
                )
                .mappings()
                .all()
            )
        return [_ep_row(r)[0] for r in rows]

    async def endpoint_for_run(
        self, *, owner_id: int, ref: str
    ) -> tuple[EndpointRow, dict[str, str]]:
        """Ряд + РАСПЕЧАТАННЫЕ секреты — только на момент вызова, в журнал не попадают."""
        async with self._session() as s:
            r = (
                (
                    await s.execute(
                        text(
                            f"SELECT {_EP_COLS} FROM automation.endpoint WHERE owner_id = :o"  # noqa: S608
                            " AND (id::text LIKE :ref || '%' OR name ILIKE :ref || '%')"
                            " ORDER BY (id::text LIKE :ref || '%') DESC, name LIMIT 1"
                        ),
                        {"o": int(owner_id), "ref": ref.strip().lower()},
                    )
                )
                .mappings()
                .first()
            )
        if r is None:
            raise KeyError(ref)
        return _ep_row(r, include_secrets=True)

    async def drop_endpoint(self, *, owner_id: int, ref: str) -> bool:
        async with self._session() as s:
            res = await s.execute(
                text(
                    "DELETE FROM automation.endpoint WHERE id = (SELECT id FROM"
                    " automation.endpoint WHERE owner_id = :o AND (id::text LIKE :ref ||"
                    " '%' OR name ILIKE :ref || '%') ORDER BY (id::text LIKE :ref || '%')"
                    " DESC, name LIMIT 1)"
                ),
                {"o": int(owner_id), "ref": ref.strip().lower()},
            )
            await s.commit()
        return bool((res.rowcount or 0) > 0)

    async def record_run(
        self,
        *,
        endpoint_id: str,
        owner_id: int,
        ok: bool,
        status: int,
        ms: int,
        digest: str,
        triggered_by: str = "model",
    ) -> None:
        async with self._session() as s:
            await s.execute(
                text(
                    "INSERT INTO automation.run (endpoint_id, owner_id, ok, status, ms, digest,"
                    " triggered_by) VALUES (:e, :o, :ok, :st, :ms, :d, :tb)"
                ),
                {
                    "e": endpoint_id,
                    "o": int(owner_id),
                    "ok": bool(ok),
                    "st": int(status),
                    "ms": int(ms),
                    "d": digest[:1000],
                    "tb": triggered_by,
                },
            )
            await s.execute(
                text(
                    "UPDATE automation.endpoint SET last_run = now(), last_status ="
                    " CASE WHEN :ok THEN 'ok ' || :stt ELSE 'fail ' || :stt END"
                    " WHERE id = :e"
                ),
                {"ok": bool(ok), "stt": str(int(status)), "e": endpoint_id},
            )
            await s.commit()

    async def recent_runs(self, *, owner_id: int, limit: int = 10) -> list[dict[str, Any]]:
        async with self._session() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            "SELECT r.ok, r.status, r.ms, r.digest, r.triggered_by, e.name"
                            " FROM automation.run r JOIN automation.endpoint e ON e.id ="
                            " r.endpoint_id WHERE r.owner_id = :o ORDER BY r.id DESC LIMIT :n"
                        ),
                        {"o": int(owner_id), "n": int(limit)},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    # ---------- входящие вебхуки ----------

    async def add_hook(
        self, *, owner_id: int, name: str, policy: str = "notify", rate_per_min: int = 6
    ) -> tuple[HookRow, str]:
        """Возвращает (ряд, СЕКРЕТ). Секрет показывается ровно один раз — на создании."""
        name = name.strip().lower()
        if not _HOOK_NAME_RE.match(name):
            raise ValueError("имя вебхука: строчные [a-z0-9_-], 3..41 символа")
        if policy not in ("notify", "turn"):
            raise ValueError("policy: notify|turn")
        secret = _secrets.token_urlsafe(24)
        if _cipher() is None:
            raise ValueError("ключарки нет — секрет вебхука прятать нечем; AEGIS_MASTER_KEY")
        async with self._session() as s:
            clash = (
                await s.execute(
                    text("SELECT 1 FROM automation.hook WHERE name = :n LIMIT 1"), {"n": name}
                )
            ).first()
            if clash is not None:
                raise ValueError(f"имя «{name}» уже занято (свободны у всех владельцев)")
            row = (
                (
                    await s.execute(
                        text(
                            "INSERT INTO automation.hook (owner_id, name, secret, policy,"
                            " rate_per_min) VALUES (:o, :n, :sec, :p, :r) RETURNING id, owner_id,"
                            " name, policy, rate_per_min, enabled, fires, last_fire"
                        ),
                        {
                            "o": int(owner_id),
                            "n": name,
                            "sec": _seal(secret),
                            "p": policy,
                            "r": max(0, min(120, int(rate_per_min))),
                        },
                    )
                )
                .mappings()
                .one()
            )
            await s.commit()
        return _hook_row(row), secret

    async def list_hooks(self, *, owner_id: int) -> list[HookRow]:
        async with self._session() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            "SELECT id, owner_id, name, policy, rate_per_min, enabled, fires,"
                            " last_fire FROM automation.hook WHERE owner_id = :o ORDER BY name"
                        ),
                        {"o": int(owner_id)},
                    )
                )
                .mappings()
                .all()
            )
        return [_hook_row(r) for r in rows]

    async def hook_by_name(self, name: str) -> tuple[HookRow, str] | None:
        """Для приёмника: ряд + открытый секрет (имя глобально уникально — владелец в ряду)."""
        async with self._session() as s:
            r = (
                (
                    await s.execute(
                        text(
                            "SELECT id, owner_id, name, secret, policy, rate_per_min, enabled,"
                            " fires, last_fire FROM automation.hook WHERE name = :n"
                        ),
                        {"n": name.strip().lower()},
                    )
                )
                .mappings()
                .first()
            )
        if r is None:
            return None
        try:
            secret = _open(str(r["secret"]))
        except (ValueError, RuntimeError):
            return _hook_row(r), ""
        return _hook_row(r), secret

    async def set_hook_enabled(self, *, owner_id: int, ref: str, enabled: bool) -> str | None:
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "UPDATE automation.hook SET enabled = :en, updated_at = now()"
                            " WHERE id = (SELECT id FROM automation.hook WHERE owner_id = :o"
                            " AND (id::text LIKE :ref || '%' OR name ILIKE :ref || '%')"
                            " ORDER BY (id::text LIKE :ref || '%') DESC, name LIMIT 1)"
                            " RETURNING name"
                        ),
                        {"en": bool(enabled), "o": int(owner_id), "ref": ref.strip().lower()},
                    )
                )
                .mappings()
                .first()
            )
            await s.commit()
        verb = "включен" if enabled else "выключен"
        return f"«{row['name']}» {verb}" if row else None

    async def bump_fire(self, hook_id: str) -> None:
        async with self._session() as s:
            await s.execute(
                text(
                    "UPDATE automation.hook SET fires = fires + 1, last_fire = now() WHERE id = :i"
                ),
                {"i": hook_id},
            )
            await s.commit()

    # ---------- сводка для doctor ----------

    async def counts(self) -> dict[str, int]:
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "SELECT (SELECT count(*) FROM automation.endpoint)::int AS endpoints,"
                            " (SELECT count(*) FROM automation.hook WHERE enabled)::int AS hooks,"
                            " (SELECT count(*) FROM automation.run WHERE ok = false AND id >"
                            " coalesce((SELECT max(id) - 100 FROM automation.run), 0))::int AS"
                            " recent_fails"
                        )
                    )
                )
                .mappings()
                .one()
            )
        return dict(row)


def _hook_row(r: Any) -> HookRow:
    return HookRow(
        id=str(r["id"]),
        owner_id=int(r["owner_id"]),
        name=str(r["name"]),
        policy=str(r["policy"]),
        rate_per_min=int(r["rate_per_min"]),
        enabled=bool(r["enabled"]),
        fires=int(r["fires"]),
        last_fire=r["last_fire"],
    )
