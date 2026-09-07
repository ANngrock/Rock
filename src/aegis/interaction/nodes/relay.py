"""NATS-обвязка узлов: бот — очередь+публикации, демон — подписка+исполнение.

Факт остаётся в БД (planning/nodes), NATS — труба с at-least-once семантикой: повтор команды
гасится кольцом идемпотентности демона, потеря ответа — состоянием 'lost'/'failed' на сервере.
Отсюда прямое следствие: публикации бота идут в core-режиме (без JetStream-ack) — «не доставили»
здесь дешевле «доставили дважды и не заметили», а «доставили дважды» демон уже исключает.

Строки субъект-схемы (аegis.node.<id>.cmd|res|hb, aegis.node.pair) — протокол между двумя
нашими процессами; меняется вместе, тестами на формат зафиксирована.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any

import structlog

from aegis.planning.nodes import SqlNodeStore

__all__ = ["NodeGateway", "node_id_from_subject", "subject_cmd", "subject_hb", "subject_res"]

log = structlog.get_logger(__name__)

PAIR_SUBJECT = "aegis.node.pair"


def subject_cmd(node_id: str) -> str:
    return f"aegis.node.{node_id}.cmd"


def subject_res(node_id: str) -> str:
    return f"aegis.node.{node_id}.res"


def subject_hb(node_id: str) -> str:
    return f"aegis.node.{node_id}.hb"


def node_id_from_subject(subject: str) -> str | None:
    parts = subject.split(".")
    if len(parts) == 4 and parts[0] == "aegis" and parts[1] == "node":  # noqa: PLR2004
        return parts[2]
    return None


class NodeGateway:
    """Сторона бота: подписка на res/hb/pair + рассылка созревших команд. Один цикл, одна задача."""

    def __init__(self, *, cfg: Any, store: SqlNodeStore | None = None) -> None:
        self._cfg = cfg
        self.store = store or SqlNodeStore()
        self._nc: Any = None
        self._stop = asyncio.Event()

    @property
    def url(self) -> str:
        return str(getattr(self._cfg, "nats_url", "") or "").strip()

    async def start(self) -> None:
        try:
            import nats
        except ImportError as exc:
            raise RuntimeError('нужен nats-py: pip install -e ".[durable]"') from exc
        self._nc = await nats.connect(
            servers=[self.url], connect_timeout=3.0, allow_reconnect=True, name="aegis-bot-nodes"
        )
        await self._nc.subscribe("aegis.node.*.res", cb=self._on_result)
        await self._nc.subscribe("aegis.node.*.hb", cb=self._on_heartbeat)
        await self._nc.subscribe(PAIR_SUBJECT, cb=self._on_pair)

    async def aclose(self) -> None:
        if self._nc is not None:
            with contextlib.suppress(Exception):
                await self._nc.drain()
            self._nc = None

    async def _publish(self, subject: str, msg: dict[str, Any]) -> None:
        if self._nc is None:
            raise RuntimeError("NATS не подключён")
        await self._nc.publish(subject, json.dumps(msg, ensure_ascii=False).encode("utf-8"))

    async def run(self) -> None:
        """Главный цикл: каждые ~5 с разобрать очередь; подписки живут в nats-py задачах."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5)
            except TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self._dispatch_once()
            except Exception as exc:  # noqa: BLE001 - узлы не имеют права валить бота
                log.warning("nodes.dispatch_failed", err=repr(exc)[:200])
                await asyncio.sleep(5)

    def stop(self) -> None:
        self._stop.set()

    async def _dispatch_once(self) -> None:
        if self._nc is None:
            await self.start()
        for cmd, node_name, decision in await self.store.claim_for_dispatch(limit=10):
            if decision == "send":
                try:
                    await self._publish(
                        subject_cmd(cmd.node_id),
                        {"id": cmd.id, "action": cmd.action, "payload": cmd.payload},
                    )
                    log.info(
                        "nodes.command_sent", id=cmd.short_id, node=node_name, action=cmd.action
                    )
                except Exception as exc:  # noqa: BLE001 - команда уже dispatched: потеряется как lost
                    log.warning("nodes.publish_failed", id=cmd.short_id, err=repr(exc)[:160])
            elif decision in ("expired", "lost"):
                await self._note_owner(
                    cmd.owner_id, f"💻 команда {cmd.short_id} ({cmd.action}): {decision}"
                )

    async def _on_pair(self, msg: Any) -> None:
        data = _decode(msg)
        name, code = str(data.get("name") or "")[:64], str(data.get("code") or "")
        if not name or not code:
            return
        ok = await self.store.pair(name=name, code=code)
        node = await self.store.find(owner_id=int(self._cfg.telegram_owner_id or 1), ref=name)
        await self._publish_safe(
            f"aegis.node.{node.id if node else 'unknown'}.ack",
            {"ok": ok, "node_id": node.id if (ok and node) else None},
        )
        log.info("nodes.pair", name=name, ok=ok)
        if ok:
            await self._note_owner(node.owner_id if node else 0, f"💻 узел «{name}» привязан")

    async def _on_heartbeat(self, msg: Any) -> None:
        node_id = node_id_from_subject(msg.subject)
        if node_id is None:
            return
        data = _decode(msg)
        if data.get("node_id") != node_id:
            return
        with contextlib.suppress(Exception):
            await self.store.touch(node_id=node_id, caps=dict(data.get("caps") or {}))

    async def _on_result(self, msg: Any) -> None:
        data = _decode(msg)
        cmd_id = str(data.get("id") or "")
        if not cmd_id:
            return
        owner_id = await self.store.settle(
            command_id=cmd_id,
            ok=bool(data.get("ok")),
            result=str(data.get("result") or "")[:8000] or None,
            error=str(data.get("error") or "")[:800] or None,
        )
        if owner_id is None:
            return  # запоздалый/повторный — чат не дублируем
        from aegis.planning.nodes import build_result_note

        note = build_result_note(
            str(data.get("action") or "run"), data.get("result"), data.get("error")
        )
        b64 = str(data.get("image_b64") or "")
        await self._note_owner(owner_id, note, image_b64=b64 or None)

    async def _note_owner(self, owner_id: int, text: str, *, image_b64: str | None = None) -> None:
        try:
            from aegis.interaction.telegram.notify import TelegramNotifier

            notifier = TelegramNotifier.from_settings(self._cfg)
            await notifier.start()
            try:
                await notifier.send_note(text, image_b64=image_b64)
            finally:
                await notifier.aclose()
        except Exception as exc:  # noqa: BLE001 - без токена/сети факт живёт в БД, это заметно в логе
            log.warning("nodes.notify_failed", err=repr(exc)[:200])

    async def _publish_safe(self, subject: str, msg: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await self._publish(subject, msg)


def _decode(msg: Any) -> dict[str, Any]:
    try:
        data = json.loads(bytes(msg.data).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


@dataclass(slots=True)
class NodeDaemon:
    """Сторона машины владельца: исходящее соединение, никакой прослушки портов."""

    url: str
    name: str
    code: str | None = None
    node_id: str | None = None
    hb_seconds: float = 20.0
    _nc: Any = None

    async def serve(self) -> None:
        try:
            import nats
        except ImportError as exc:  # pragma: no cover - сообщение для человека
            raise SystemExit('нужен nats-py: pip install -e "aegis[durable]"') from exc
        from aegis.interaction.nodes.actions import ExecutedRing, execute, system_snapshot

        self._nc = await nats.connect(
            servers=[self.url],
            connect_timeout=5.0,
            allow_reconnect=True,
            name=f"aegis-node-{self.name}",
        )
        executed = ExecutedRing()
        state: dict[str, Any] = {"node_id": self.node_id, "subscribed": None, "rejected": None}

        async def on_ack(msg: Any) -> None:
            data = _decode(msg)
            if data.get("ok") and data.get("node_id"):
                state["node_id"] = str(data["node_id"])
                print(f"узел привязан: id={state['node_id'][:8]}")
            else:
                state["rejected"] = (
                    "привязка отклонена: код неверный/истёк или узел не в pending "
                    "(aegis node enroll NAME заново)"
                )

        async def on_cmd(msg: Any) -> None:
            data = _decode(msg)
            cmd_id = str(data.get("id") or "")
            if not cmd_id or not executed.remember_if_new(cmd_id):
                return
            payload_cmd = {
                "action": str(data.get("action") or "system"),
                "payload": dict(data.get("payload") or {}),
            }
            result = await asyncio.to_thread(execute, payload_cmd)
            body: dict[str, Any] = {
                "id": cmd_id,
                "action": result.get("action"),
                "ok": bool(result.get("ok")),
                "result": str(result.get("result") or "")[:8000] or None,
                "error": result.get("error"),
            }
            res_text = str(result.get("result") or "")
            if result.get("action") == "screenshot" and "\n" in res_text:
                path, _, b64 = res_text.partition("\n")
                body["result"] = path
                body["image_b64"] = b64
            node = state["node_id"] or ""
            await self._nc.publish(
                f"aegis.node.{node}.res", json.dumps(body, ensure_ascii=False).encode()
            )

        await self._nc.subscribe("aegis.node.pair.ack", cb=on_ack)
        if self.code:
            await self._nc.publish(
                PAIR_SUBJECT,
                json.dumps({"name": self.name, "code": self.code}).encode("utf-8"),
            )
        while True:
            if state["rejected"]:
                raise SystemExit(state["rejected"])
            node = state["node_id"]
            if not node:
                await asyncio.sleep(1)
                continue
            if state["subscribed"] != node:
                # ровно одна подписка: переподписка на каждый heartbeat удвоила бы ряд
                await self._nc.subscribe(subject_cmd(node), cb=on_cmd)
                state["subscribed"] = node
            hb = {"node_id": node, "caps": system_snapshot()}
            await self._nc.publish(subject_hb(node), json.dumps(hb).encode())
            await asyncio.sleep(self.hb_seconds)
