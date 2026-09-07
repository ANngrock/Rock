"""Узлы: состояние машины вне сервера живёт здесь; транспорт (NATS) — снаружи домена.

Почему очередь, а не «отправил команду в брокер»: исполнение на чужом ноутбуке — это
неизвестная задержка и неизвестный финал. Строка-намерение с машиной состояний отвечает на
вопрос «что с командой сейчас» из БД одним запросом; брокер, упавший посреди полёта, не
создаёт «команду-призрак, возможно исполненную».

Чистые функции (decide_*, build_result_note) существуют отдельно от SQL специально: всю
арифметику состояний владелец может прочитать и протестировать без базы — ровно та причина,
по которой она не живёт внутри UPDATE ... CASE.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text

__all__ = [
    "NODE_ACTIONS",
    "Node",
    "NodeCommand",
    "PairingCode",
    "SqlNodeStore",
    "build_result_note",
    "decide_dispatch",
    "validate_payload",
]

log = structlog.get_logger(__name__)

NODE_ACTIONS = ("system", "notify", "screenshot", "run")
PAIR_TTL = timedelta(minutes=15)
#: dispatched без ответа дольше этого — не «перепоставить в очередь» (run не идемпотентен!),
#: а честно сказать владельцу «неизвестно, исполнилась ли; проверь на машине»
DISPATCH_LOST = timedelta(minutes=5)
#: узел не на связи дольше — «офлайн», команды в полёт не отправляем
ONLINE_STALE = timedelta(seconds=90)


@dataclass(slots=True, frozen=True)
class Node:
    id: str
    owner_id: int
    name: str
    status: str
    last_seen: datetime | None
    caps: dict[str, Any]

    @property
    def short_id(self) -> str:
        return self.id[:8]


@dataclass(slots=True, frozen=True)
class NodeCommand:
    id: str
    node_id: str
    owner_id: int
    action: str
    payload: dict[str, Any]
    status: str
    created_at: datetime
    expires_at: datetime

    @property
    def short_id(self) -> str:
        return self.id[:8]


class PairingCode:
    """Код связки: 6 цифр, в БД — только sha256. Звучит банально и правильно: короче — брутфорс,
    длиннее — переписывание с экрана телефона пальцами."""

    _RE = re.compile(r"^\d{6}$")

    @staticmethod
    def generate() -> str:
        import secrets

        return f"{secrets.randbelow(1_000_000):06d}"

    @classmethod
    def hash(cls, code: str) -> str:
        if not cls._RE.fullmatch(code or ""):
            raise ValueError("код — ровно 6 цифр")
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

    @classmethod
    def verify(cls, code: str, stored_hash: str | None) -> bool:
        if not stored_hash:
            return False
        try:
            candidate = cls.hash(code)
        except ValueError:
            return False
        import hmac

        return hmac.compare_digest(candidate, stored_hash)


def validate_payload(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Форма команды по action — до БД и до узла. Возвращает канон (только известные поля)."""
    if action not in NODE_ACTIONS:
        raise ValueError(f"действие обязано быть одним из {NODE_ACTIONS}")
    if action == "system":
        return {}
    if action == "notify":
        text = " ".join(str(payload.get("text") or "").split())[:500]
        if len(text) < 1:
            raise ValueError("notify: пустой текст")
        return {"text": text}
    if action == "screenshot":
        return {"display": str(payload.get("display") or "auto")[:16]}
    command = str(payload.get("command") or "").strip()
    if not command:
        raise ValueError("run: пустая команда")
    if len(command) > 2000:  # noqa: PLR2004 — длиннее — это скрипт, а не команда; скрипты — плагин
        raise ValueError("run: команда длиннее 2000 символов")
    timeout = int(payload.get("timeout_s") or 60)
    if not 1 <= timeout <= 300:
        raise ValueError("run: timeout_s — от 1 до 300 секунд")
    return {"command": command, "timeout_s": timeout}


def decide_dispatch(
    *,
    now: datetime,
    status: str,
    node_status: str,
    node_last_seen: datetime | None,
    dispatched_at: datetime | None,
    expires_at: datetime,
    online_stale: timedelta = ONLINE_STALE,
    dispatch_lost: timedelta = DISPATCH_LOST,
) -> str:
    """Что делать с командой на этом ходу тика: send|wait|expired|lost|parked.

    'parked' — узел офлайн: команда ждёт (до expires_at) в очереди, а не считается упавшей:
    ноутбук спит — это не ошибка владельца и не ошибка демона."""
    if status in ("done", "failed", "expired", "cancelled"):
        return "settled"
    if now >= expires_at:
        return "expired"
    if node_status != "paired":
        return "parked"
    online = node_last_seen is not None and (now - node_last_seen) <= online_stale
    if status == "queued":
        return "send" if online else "parked"
    # dispatched: потеряли ответ?
    if dispatched_at is not None and (now - dispatched_at) > dispatch_lost:
        return "lost"
    return "wait" if online else "wait"


def build_result_note(cmd_action: str, result: str | None, error: str | None) -> str:
    """Строка для чата владельца. Ограничена, потому что вывод чужой машины — не лог на весь
    экран: 1500 символов хвоста с головой важнее цельности."""
    head = {
        "system": "система",
        "notify": "уведомление доставлено",
        "screenshot": "скриншот",
        "run": "команда",
    }[cmd_action]
    if error:
        return f"💻 {head}: ! {error[:400]}"
    body = (result or "").strip()
    if len(body) > 1500:  # noqa: PLR2004 — размер сообщения Telegram с запасом
        body = body[:600] + "\n…[срезано]…\n" + body[-400:]
    return f"💻 {head}:\n{body}" if body else f"💻 {head}: ok"


def caps_summary(caps: dict[str, Any]) -> str:
    bits = [str(caps.get("os") or "?"), str(caps.get("hostname") or "")]
    if caps.get("online") is True:
        bits.append("онлайн")
    return " ".join(x for x in bits if x).strip()


_SELECT_NODE = """
    SELECT id::text AS id, owner_id, name, status, last_seen, caps::text AS caps
      FROM planning.nodes
"""


def _node(row: Any) -> Node:
    caps_raw = row["caps"]
    caps = json.loads(caps_raw) if isinstance(caps_raw, str) else dict(caps_raw or {})
    return Node(
        id=str(row["id"]),
        owner_id=int(row["owner_id"]),
        name=str(row["name"]),
        status=str(row["status"]),
        last_seen=row["last_seen"],
        caps=caps,
    )


class SqlNodeStore:
    """Транзакция на вызов; все переходы состояний — атомарные UPDATE с защитой от гонки
    (WHERE включает ожидаемое текущее состояние: два тика не отправят одну команду дважды)."""

    def __init__(self, *, session_factory: Any = None) -> None:
        self._sm = session_factory

    def _session(self) -> Any:
        from aegis.platform.db import session

        return self._sm() if self._sm is not None else session()

    # --- узлы ---

    async def enroll(self, *, owner_id: int, name: str) -> tuple[str, str]:
        """(node_id, код) — код существует ровно в момент выдачи и в БД не лежит."""
        code = PairingCode.generate()
        async with self._session() as s:
            row = (
                await s.execute(
                    text(
                        "INSERT INTO planning.nodes (owner_id, name, status, pairing_hash,"
                        " pair_expires_at) VALUES (:o, :n, 'pending', :h, now() +"
                        " interval '15 minutes') ON CONFLICT (owner_id, name) DO UPDATE SET"
                        " status = 'pending', pairing_hash = EXCLUDED.pairing_hash,"
                        " pair_expires_at = EXCLUDED.pair_expires_at, updated_at = now()"
                        " RETURNING id::text AS id"
                    ),
                    {"o": int(owner_id), "n": name, "h": PairingCode.hash(code)},
                )
            ).mappings()
            node_id = str(row.one()["id"])
            await s.commit()
        return node_id, code

    async def pair(self, *, name: str, code: str) -> bool:
        now = datetime.now(UTC)
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "SELECT id::text AS id, pairing_hash, pair_expires_at"
                            " FROM planning.nodes WHERE name = :n AND status = 'pending'"
                        ),
                        {"n": name},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                await s.commit()
                return False
            if row["pair_expires_at"] is not None and row["pair_expires_at"] < now:
                await s.execute(
                    text(
                        "UPDATE planning.nodes SET status = 'revoked', pairing_hash = NULL"
                        " WHERE id = CAST(:i AS uuid) AND status = 'pending'"
                    ),
                    {"i": row["id"]},
                )
                await s.commit()
                return False
            if not PairingCode.verify(code, row["pairing_hash"]):
                # неверный код pending-узла не «накапливается»: брутфорс по 10^6 — это DoS чатом,
                # поэтому кривой код тоже закрывает слот, и узел перевыпускается владельцем
                await s.execute(
                    text(
                        "UPDATE planning.nodes SET status = 'revoked', pairing_hash = NULL"
                        " WHERE id = CAST(:i AS uuid) AND status = 'pending'"
                    ),
                    {"i": row["id"]},
                )
                await s.commit()
                return False
            updated = (
                await s.execute(
                    text(
                        "UPDATE planning.nodes SET status = 'paired', paired_at = now(),"
                        " pairing_hash = NULL, pair_expires_at = NULL, last_seen = now(),"
                        " updated_at = now() WHERE id = CAST(:i AS uuid) AND status = 'pending'"
                    ),
                    {"i": row["id"]},
                )
            ).rowcount
            await s.commit()
        return bool(updated)

    async def revoke(self, *, owner_id: int, name: str) -> bool:
        async with self._session() as s:
            updated = (
                await s.execute(
                    text(
                        "UPDATE planning.nodes SET status = 'revoked', last_seen = NULL,"
                        " updated_at = now() WHERE owner_id = :o AND name = :n"
                        " AND status IN ('pending', 'paired')"
                    ),
                    {"o": int(owner_id), "n": name},
                )
            ).rowcount
            await s.commit()
        return bool(updated)

    async def touch(self, *, node_id: str, caps: dict[str, Any]) -> None:
        caps_json = json.dumps(caps, ensure_ascii=False, default=str)[:4000]
        async with self._session() as s:
            await s.execute(
                text(
                    "UPDATE planning.nodes SET last_seen = now(),"
                    " caps = CAST(:c AS jsonb), updated_at = now()"
                    " WHERE id = CAST(:i AS uuid) AND status = 'paired'"
                ),
                {"c": caps_json, "i": node_id},
            )
            await s.commit()

    async def find(self, *, owner_id: int, ref: str) -> Node | None:
        """ref — имя или начало id (узел один на владельца по имени, id-префикс для точности).

        Условия всегда объединены: имя вида `c-3f9a...` совпадает с маской uuid и не должно
        терять ветку `name = :r` — владелец вводит то, что видит в `node list`."""
        sql = _SELECT_NODE + " WHERE owner_id = :o AND (id::text LIKE :r || '%' OR name = :r)"
        async with self._session() as s:
            row = (
                (await s.execute(text(sql), {"o": int(owner_id), "r": ref[:64]})).mappings().first()
            )
        return None if row is None else _node(row)

    async def list_nodes(self, *, owner_id: int) -> list[Node]:
        async with self._session() as s:
            rows = (
                (
                    await s.execute(
                        text(_SELECT_NODE + " WHERE owner_id = :o ORDER BY name"),
                        {"o": int(owner_id)},
                    )
                )
                .mappings()
                .all()
            )
        return [_node(r) for r in rows]

    async def paired_nodes_online(self) -> list[Node]:
        sql = _SELECT_NODE + " WHERE status = 'paired'"
        async with self._session() as s:
            rows = (await s.execute(text(sql))).mappings().all()
        return [_node(r) for r in rows]

    # --- команды ---

    async def enqueue(
        self,
        *,
        node: Node,
        owner_id: int,
        action: str,
        payload: dict[str, Any],
        trace_id: str | None = None,
        ttl_seconds: int = 600,
    ) -> str:
        clean = validate_payload(action, payload)
        import json as _json

        async with self._session() as s:
            row = (
                await s.execute(
                    text(
                        "INSERT INTO planning.node_commands (node_id, owner_id, action, payload,"
                        " trace_id, expires_at) VALUES (CAST(:n AS uuid), :o, :a,"
                        " CAST(:p AS jsonb), :t, now() + make_interval(secs => :ttl))"
                        " RETURNING id::text AS id"
                    ),
                    {
                        "n": node.id,
                        "o": int(owner_id),
                        "a": action,
                        "p": _json.dumps(clean, ensure_ascii=False),
                        "t": trace_id,
                        "ttl": int(ttl_seconds),
                    },
                )
            ).mappings()
            cmd_id = str(row.one()["id"])
            await s.commit()
        return cmd_id

    async def claim_for_dispatch(self, *, limit: int = 10) -> list[tuple[NodeCommand, str, str]]:
        """(команда, node_name, решение) по всем открытым командам: одна транзакция на чтение
        состояний узлов и самих команд — «узел сменил статус между запросами» не существует."""
        sql = """
            WITH due AS (
                SELECT c.id FROM planning.node_commands c
                  JOIN planning.nodes n ON n.id = c.node_id
                 WHERE c.status IN ('queued', 'dispatched')
                 ORDER BY c.created_at
                 LIMIT :limit
                 FOR UPDATE OF c SKIP LOCKED
            )
            SELECT c.id::text AS cid, c.node_id::text AS node_id, n.name AS node_name,
                   n.owner_id, c.action, c.payload::text AS payload, c.status,
                   c.created_at, c.expires_at, c.dispatched_at,
                   n.status AS node_status, n.last_seen
              FROM planning.node_commands c
              JOIN planning.nodes n ON n.id = c.node_id
              JOIN due ON due.id = c.id
        """
        async with self._session() as s:
            rows = (await s.execute(text(sql), {"limit": int(limit)})).mappings().all()
            out: list[tuple[NodeCommand, str, str]] = []
            now = datetime.now(UTC)
            for r in rows:
                payload_raw = r["payload"]
                payload = (
                    json.loads(payload_raw)
                    if isinstance(payload_raw, str)
                    else dict(payload_raw or {})
                )
                cmd = NodeCommand(
                    id=str(r["cid"]),
                    node_id=str(r["node_id"]),
                    owner_id=int(r["owner_id"]),
                    action=str(r["action"]),
                    payload=payload,
                    status=str(r["status"]),
                    created_at=r["created_at"],
                    expires_at=r["expires_at"],
                )
                decision = decide_dispatch(
                    now=now,
                    status=cmd.status,
                    node_status=str(r["node_status"]),
                    node_last_seen=r["last_seen"],
                    dispatched_at=r["dispatched_at"],
                    expires_at=cmd.expires_at,
                )
                if decision == "send":
                    await s.execute(
                        text(
                            "UPDATE planning.node_commands SET status = 'dispatched',"
                            " dispatched_at = now() WHERE id = CAST(:i AS uuid)"
                            " AND status = 'queued'"
                        ),
                        {"i": cmd.id},
                    )
                elif decision == "expired":
                    await s.execute(
                        text(
                            "UPDATE planning.node_commands SET status = 'expired', done_at = now()"
                            " WHERE id = CAST(:i AS uuid) AND status IN ('queued', 'dispatched')"
                        ),
                        {"i": cmd.id},
                    )
                elif decision == "lost":
                    await s.execute(
                        text(
                            "UPDATE planning.node_commands SET status = 'failed', done_at = now(),"
                            " error = 'ответ узла потерян: команда могла исполниться —"
                            " проверьте на машине'"
                            " WHERE id = CAST(:i AS uuid) AND status = 'dispatched'"
                        ),
                        {"i": cmd.id},
                    )
                out.append((cmd, str(r["node_name"]), decision))
            await s.commit()
        return out

    async def settle(
        self, *, command_id: str, ok: bool, result: str | None, error: str | None
    ) -> int | None:
        """Финал команды. Возвращает owner_id (для ответа в чат) ровно у одного победителя —
        повторный/запоздалый результат не должен второй раз писать в чат."""
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(
                            "UPDATE planning.node_commands SET"
                            " status = CASE WHEN :ok THEN 'done' ELSE 'failed' END,"
                            " result = :res, error = :err, done_at = now()"
                            " WHERE id = CAST(:i AS uuid) AND status = 'dispatched'"
                            " RETURNING owner_id"
                        ),
                        {
                            "ok": bool(ok),
                            "res": (result or "")[:8000] or None,
                            "err": (error or "")[:800] or None,
                            "i": command_id,
                        },
                    )
                )
                .mappings()
                .first()
            )
            owner_id = int(row["owner_id"]) if row is not None else None
            await s.commit()
        return owner_id

    async def list_recent(self, *, owner_id: int, limit: int = 10) -> list[dict[str, Any]]:
        async with self._session() as s:
            rows = (
                (
                    await s.execute(
                        text(
                            "SELECT c.id::text AS id, n.name AS node, c.action, c.status,"
                            " c.error, c.done_at, left(c.result, 240) AS result_preview"
                            " FROM planning.node_commands c"
                            " JOIN planning.nodes n ON n.id = c.node_id"
                            " WHERE c.owner_id = :o ORDER BY c.created_at DESC LIMIT :l"
                        ),
                        {"o": int(owner_id), "l": int(limit)},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    async def cancel(self, *, owner_id: int, ref: str) -> bool:
        async with self._session() as s:
            updated = (
                await s.execute(
                    text(
                        "UPDATE planning.node_commands SET status = 'cancelled', done_at = now()"
                        " WHERE owner_id = :o AND id::text LIKE :r || '%' AND status = 'queued'"
                    ),
                    {"o": int(owner_id), "r": ref[:36]},
                )
            ).rowcount
            await s.commit()
        return bool(updated)
