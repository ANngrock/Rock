"""Демон на машине владельца: Telethon-клиент его личного аккаунта.

Чтобы «бот видел личные чаты», в Telegram есть ровно один путь — сессия самого пользователя
(MTProto). Это привилегия с ценой: аккаунт может получить ограничения за спам-поведение,
и поэтому демон запускается вручную, явным человеком, с явным осознанием (USERBOT_ENABLED).

Демон ходит НАРУЖУ дважды: к Telegram (его протокол) и к вашему NATS (публикации/подписка).
Никаких входящих портов. При входящем сообщении он пишет строку в инбокс-поток; решение о
черновиках/отправке принимает шлюз бота, не демон — на машине владельца не хранится ни политика,
ни LLM-ключи.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass, field
from typing import Any

import structlog

from aegis.interaction.userbridge.relay import (
    HB_SUBJECT,
    IN_SUBJECT,
    subj_cmd,
    subj_res,
)

__all__ = ["UserbotDaemon"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class UserbotDaemon:
    name: str
    nats_url: str
    api_id: int
    api_hash: str
    session_path: str = "aegis-userbot.session"
    phone: str | None = None
    hb_seconds: float = 45.0
    max_message_chars: int = 4000
    _nc: Any = field(default=None, repr=False)
    _client: Any = field(default=None, repr=False)
    _seen_ids: list[int] = field(default_factory=list, repr=False)

    async def serve(self, *, list_chats: bool = False) -> None:
        try:
            import nats
            from telethon import TelegramClient, events
        except ImportError as exc:  # pragma: no cover - сообщение для человека, не для теста
            raise RuntimeError(
                'для юзербота нужны пакеты: pip install -e ".[durable,userbot]"'
            ) from exc
        client = TelegramClient(self.session_path, self.api_id, self.api_hash)
        self._nc = await nats.connect(
            servers=[self.nats_url],
            connect_timeout=5.0,
            allow_reconnect=True,
            name=f"aegis-ub-{self.name}",
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.start(
                    phone=self.phone
                )  # интерактив: телефон/код/2FA — только у владельца
            self._client = client
            if list_chats:
                await self._print_chats(client)
                return

            client.add_event_handler(
                self._publish_incoming, events.NewMessage(incoming=True)
            )  # обработчик без декоратора: чужая untyped-обвязка не трогает наши типы

            await self._nc.subscribe(subj_cmd(self.name), cb=self._on_cmd)
            hb_task = asyncio.create_task(self._heartbeat_loop(client))
            try:
                await asyncio.Event().wait()  # до SIGINT: демон не «проверяет флаги», он ждёт
            finally:
                hb_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb_task
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()
            with contextlib.suppress(Exception):
                await self._nc.drain()

    async def _print_chats(self, client: Any) -> None:
        print("Чаты (id — для aegis userbot watch <id> --mode ...):")
        for dialog in await client.get_dialogs(limit=200):
            ent = dialog.entity
            cid = getattr(ent, "id", "?")
            title = getattr(ent, "title", None) or getattr(ent, "username", None) or "личный чат"
            kind = type(ent).__name__
            print(f"  {cid:<14} {kind:<22} {title}")

    async def _publish_incoming(self, ev: Any) -> None:
        if ev.id in self._seen_ids:  # повтор (edit/пересылка ивентов) — не вторая доставка
            return
        msg = ev.message
        text = (msg.raw_text or "").strip()
        if not text:
            kinds = [
                n
                for n, flag in (
                    ("фото", msg.photo),
                    ("файл", msg.file),
                    ("голос", msg.voice),
                    ("стикер", msg.sticker),
                )
                if flag
            ]
            text = "[медиа: " + ",".join(kinds) + "]" if kinds else "[пустое сообщение]"
        sender = None
        with contextlib.suppress(Exception):
            sender = await ev.get_sender()
        from_name = ""
        if sender is not None and getattr(sender, "id", None) not in (None, ev.chat_id):
            from_name = getattr(sender, "first_name", None) or getattr(sender, "username", "") or ""
        chat_name = ""
        with contextlib.suppress(Exception):
            chat = await ev.get_chat()
            chat_name = getattr(chat, "title", None) or getattr(chat, "username", None) or ""
        await self._publish(
            IN_SUBJECT,
            {
                "daemon": self.name,
                "chat_id": str(ev.chat_id),
                "chat_name": chat_name[:128],
                "from_name": from_name[:128],
                "text": text[: self.max_message_chars],
                "msg_id": str(ev.id),
            },
        )
        self._seen_ids.append(ev.id)
        del self._seen_ids[:-512]  # кольцо: пережить ретраи без памяти на весь диалог

    async def _on_cmd(self, msg: Any) -> None:
        try:
            data = json.loads(bytes(msg.data).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(data, dict) or data.get("cmd") != "send":
            return
        cid = str(data.get("id") or "")
        ok, err = True, ""
        try:
            target: Any = data.get("chat_id") or ""
            with contextlib.suppress(ValueError):
                target = int(target)
            await self._client.send_message(target, str(data.get("text") or "")[:4000])
        except Exception as exc:  # noqa: BLE001 - отказ телеграфа уходит владельцу, не в трейсбек
            ok, err = False, f"{type(exc).__name__}: {str(exc)[:200]}"
        await self._publish(subj_res(self.name), {"id": cid, "ok": ok, "error": err or None})

    async def _heartbeat_loop(self, client: Any) -> None:
        while True:
            await asyncio.sleep(self.hb_seconds)
            dialogs = 0
            with contextlib.suppress(Exception):
                dialogs = len(await client.get_dialogs(limit=100))
            with contextlib.suppress(Exception):
                await self._publish(
                    f"{HB_SUBJECT}.{self.name}",
                    {"daemon": self.name, "caps": {"dialogs": dialogs, "os": os.uname().sysname}},
                )

    async def _publish(self, subject: str, payload: dict[str, Any]) -> None:
        if self._nc is None:
            return
        with contextlib.suppress(Exception):
            await self._nc.publish(subject, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
