"""Реестр коннекторов: строка = одно подключение (mcp | api | plugin) с зашифрованным секретом.

Три правила, на которых держится слой:

* Секрет живит завёрнутым (BlobCipher журнала): дамп базы без env не даёт ключей, а
  `aegis connect show` печатает маску, никогда не значение.
* Конфиг — jsonb СВОБОДНОЙ формы быть не может: shape проверяется при записи (ValueError до
  SQL), иначе кривой MCP-коннектор падает молча в момент, когда владелец ждёт инструмент.
* Отключённый коннектор не участвует ни в пробе, ни в мосте инструментов: enabled — рубильник,
  а не комментарий.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from aegis.platform.crypto import BlobCipher
from aegis.platform.db import session

__all__ = [
    "CONNECTOR_KINDS",
    "Connector",
    "SqlConnectorStore",
    "validate_config",
]

CONNECTOR_KINDS = ("mcp", "api", "plugin")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


@dataclass(slots=True, frozen=True)
class Connector:
    id: str
    owner_id: int
    kind: str
    name: str
    enabled: bool
    config: dict[str, Any]
    last_error: str | None
    last_ok_at: Any

    @property
    def short_id(self) -> str:
        return self.id[:8]


def validate_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    """Форма конфига по kind. Возвращает канон (только известные поля) — мусор молча не носим."""
    if kind == "mcp":
        command = str(config.get("command") or "").strip()
        if not command:
            raise ValueError("mcp: нужен command (запускаемый файл сервера)")
        if command.startswith(("http://", "https://")):
            raise ValueError(
                "mcp v1 понимает только stdio-серверы; http-транспорт — следующий шаг, "
                "не молча игнорируем"
            )
        args = config.get("args") or []
        if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
            raise ValueError("mcp: args — список строк")
        env = config.get("env") or {}
        if not isinstance(env, dict) or any(not _ENV_NAME_RE.fullmatch(str(k)) for k in env):
            raise ValueError("mcp: env — объект с именами вида VAR_NAME")
        if str(config.get("secret_env") or "") and not _ENV_NAME_RE.fullmatch(
            str(config["secret_env"])
        ):
            raise ValueError("mcp: secret_env — имя переменной окружения (верхний регистр)")
        return {
            "command": command,
            "args": [str(a) for a in args][:32],
            "env": {str(k): str(v) for k, v in env.items()},
            "secret_env": str(config.get("secret_env") or ""),
        }
    if kind == "api":
        base_url = str(config.get("base_url") or "").strip()
        if base_url and not base_url.startswith(("http://", "https://")):
            raise ValueError("api: base_url — http(s)")
        header = str(config.get("header") or "Authorization")
        return {"base_url": base_url[:500], "header": header[:100]}
    if kind == "plugin":
        module = str(config.get("module") or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module):
            raise ValueError("plugin: module — импортируемый путь (pkg.mod)")
        return {"module": module}
    raise ValueError(f"неизвестный kind {kind!r}: умею {CONNECTOR_KINDS}")


_SELECT = """
    SELECT id::text AS id, owner_id, kind, name, enabled, config, last_error, last_ok_at
      FROM integrations.connectors
"""


def _row_connector(row: Any) -> Connector:
    cfg = row["config"]
    if isinstance(cfg, str):
        import json

        cfg = json.loads(cfg)
    return Connector(
        id=str(row["id"]),
        owner_id=int(row["owner_id"]),
        kind=str(row["kind"]),
        name=str(row["name"]),
        enabled=bool(row["enabled"]),
        config=dict(cfg or {}),
        last_error=row["last_error"],
        last_ok_at=row["last_ok_at"],
    )


class SqlConnectorStore:
    """Транзакция на вызов (контракт всех магазинов репозитория)."""

    def __init__(self, *, session_factory: Any = None, cipher: BlobCipher | None = None) -> None:
        self._sm = session_factory
        self._cipher = cipher

    def _session(self) -> Any:
        return self._sm() if self._sm is not None else session()

    def _require_cipher(self) -> BlobCipher:
        if self._cipher is None:
            from aegis.platform.config import settings
            from aegis.platform.crypto import load_keks
            from aegis.platform.vault import get_vault

            cfg = settings()
            vault = get_vault(cfg)
            self._cipher = (
                vault.cipher
                if vault is not None and vault.cipher is not None
                else BlobCipher(load_keks(cfg), active_version=cfg.crypto_key_version)
            )
        return self._cipher

    async def add(self, *, owner_id: int, kind: str, name: str, config: dict[str, Any]) -> str:
        if kind not in CONNECTOR_KINDS:
            raise ValueError(f"kind обязан быть одним из {CONNECTOR_KINDS}")
        if not _NAME_RE.fullmatch(name or ""):
            raise ValueError("имя — строчные латиница/цифры/-/_ (2..64)")
        clean = validate_config(kind, config)
        import json

        async with self._session() as s:
            row = (
                await s.execute(
                    text(
                        "INSERT INTO integrations.connectors (owner_id, kind, name, config)"
                        " VALUES (:o, :k, :n, CAST(:cfg AS jsonb))"
                        " ON CONFLICT (owner_id, kind, name) DO UPDATE"
                        "    SET config = EXCLUDED.config, last_error = NULL, updated_at = now()"
                        " RETURNING id::text AS id"
                    ),
                    {
                        "o": int(owner_id),
                        "k": kind,
                        "n": name,
                        "cfg": json.dumps(clean, ensure_ascii=False),
                    },
                )
            ).mappings()
            conn_id = str(row.one()["id"])
            await s.commit()
        return conn_id

    async def set_secret(self, *, owner_id: int, kind: str, name: str, plaintext: str) -> None:
        """Секрет коннектора: для api — значение по запросу, для mcp — значение в env процесса."""
        raw = plaintext.strip().encode("utf-8")
        if not raw:
            raise ValueError("пустой секрет не храним: удали подключение или запиши значение")
        if len(raw) > 4096:  # noqa: PLR2004 — ключ длиннее 4К — это файл, а не ключ
            raise ValueError("секрет длиннее 4096 байт")
        ct, wrapped, ver = self._require_cipher().encrypt(raw)
        async with self._session() as s:
            updated = (
                await s.execute(
                    text(
                        "UPDATE integrations.connectors SET secret_ct = :ct, secret_wrapped = :w,"
                        " secret_key_version = :v, last_error = NULL, updated_at = now()"
                        " WHERE owner_id = :o AND kind = :k AND name = :n"
                    ),
                    {"ct": ct, "w": wrapped, "v": ver, "o": int(owner_id), "k": kind, "n": name},
                )
            ).rowcount
            await s.commit()
        if not updated:
            raise ValueError(f"коннектор {kind}/{name!r} не найден")

    async def secret_for_mcp(self, *, owner_id: int, connector: Connector) -> str | None:
        """Значение секрета — процессу MCP-сервера в env, строкой наружу (CLI/доктор) не отдаём."""
        env_name = str(connector.config.get("secret_env") or "")
        if not env_name:
            return None
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "SELECT secret_ct, secret_wrapped, secret_key_version"
                            " FROM integrations.connectors"
                            " WHERE owner_id = :o AND kind = 'mcp' AND name = :n"
                        ),
                        {"o": int(connector.owner_id or owner_id), "n": connector.name},
                    )
                )
                .mappings()
                .first()
            )
        if row is None or row["secret_ct"] is None:
            return None
        plain = self._require_cipher().decrypt(
            bytes(row["secret_ct"]),
            bytes(row["secret_wrapped"]),
            key_version=row["secret_key_version"],
        )
        return plain.decode("utf-8", "replace")

    async def list(self, *, owner_id: int, enabled_only: bool = False) -> list[Connector]:
        where = "WHERE owner_id = :o" + (" AND enabled" if enabled_only else "")
        async with self._session() as s:
            rows = (
                (
                    await s.execute(
                        text(_SELECT + where + " ORDER BY kind, name"), {"o": int(owner_id)}
                    )
                )
                .mappings()
                .all()
            )
        return [_row_connector(r) for r in rows]

    async def set_enabled(self, *, owner_id: int, name: str, enabled: bool) -> bool:
        async with self._session() as s:
            updated = (
                await s.execute(
                    text(
                        "UPDATE integrations.connectors SET enabled = :e, updated_at = now()"
                        " WHERE owner_id = :o AND name = :n"
                    ),
                    {"e": bool(enabled), "o": int(owner_id), "n": name},
                )
            ).rowcount
            await s.commit()
        return bool(updated)

    async def remove(self, *, owner_id: int, name: str) -> bool:
        async with self._session() as s:
            updated = (
                await s.execute(
                    text("DELETE FROM integrations.connectors WHERE owner_id = :o AND name = :n"),
                    {"o": int(owner_id), "n": name},
                )
            ).rowcount
            await s.commit()
        return bool(updated)

    async def record_probe(self, *, connector_id: str, ok: bool, error: str | None = None) -> None:
        async with self._session() as s:
            await s.execute(
                text(
                    "UPDATE integrations.connectors SET last_error = :err,"
                    " last_ok_at = CASE WHEN :ok THEN now() ELSE last_ok_at END,"
                    " updated_at = now() WHERE id = CAST(:i AS uuid)"
                ),
                {"err": None if ok else str(error)[:500], "ok": bool(ok), "i": connector_id},
            )
            await s.commit()
