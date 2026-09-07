"""Приёмник входящих вебхуков: мини-HTTP-сервер на stdlib, без FastAPI-зоопарка.

Форма входа: ``POST /h/<имя>`` c секретом в ``X-Aegis-Key`` (или ``?key=``). Что делать с
телом — решает политика ряда: ``notify`` — текст летит владельцу как сообщение; ``turn`` —
текст инициирует агентный ход, и только тогда, когда это полезно. Тело всегда считается
нечестивым: перед показом оно оборачивается в untrusted-контейнер (wrap_untrusted), и модель
не получит от вебхура никаких прав — только информацию.

Слой намеренно маленький: rate-limit в памяти процесса (per-min deque), максимум тела из
конфига, никаких cookie/редиректов. Публичный TLS — забота реверса (RUNBOOK), этот сервер
слушает 127.0.0.1, пока владелец явно не скажет иначе.
"""

from __future__ import annotations

import asyncio
import hmac
import time
from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import structlog

from aegis.automation.store import HookRow

__all__ = ["HookOutcome", "handle_hook_request", "serve_hooks"]

log = structlog.get_logger(__name__)

_MAX_BODY_DEFAULT = 64 * 1024


@dataclass(slots=True)
class HookOutcome:
    status: int
    text: str
    hook: HookRow | None = None
    payload: str = ""

    @property
    def fired(self) -> bool:
        return self.status < 300 and self.hook is not None  # noqa: PLR2004


async def handle_hook_request(
    *,
    store: Any,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    max_body: int = _MAX_BODY_DEFAULT,
    rate_state: dict[str, deque[float]] | None = None,
) -> HookOutcome:
    """Аутентификация → лимит → политика. Чистая функция поверх store — тестируется без сокета."""
    parsed = urlparse(path)
    if method.upper() == "GET":
        if parsed.path.rstrip("/") in ("/health", "/healthz"):
            return HookOutcome(200, "ok")
        return HookOutcome(405, "только POST")
    if method.upper() != "POST":
        return HookOutcome(405, "только POST")
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2 or parts[0] != "h" or not parts[1]:
        return HookOutcome(404, "нет такого маршрута")
    name = unquote(parts[1]).lower()
    found = await store.hook_by_name(name)
    if found is None:
        return HookOutcome(404, "неизвестный вебхук")
    hook, secret = found
    if not hook.enabled:
        return HookOutcome(403, "вебхук выключен")
    qs_key = (parse_qs(parsed.query).get("key") or [""])[0]
    given = headers.get("x-aegis-key") or qs_key
    if not secret or not hmac.compare_digest(str(secret), str(given)[:256]):
        return HookOutcome(401, "ключ не подошёл")
    if len(body) > max_body:
        return HookOutcome(413, f"слишком большое тело (макс {max_body // 1024} КиБ)")
    if hook.rate_per_min > 0:
        state = rate_state if rate_state is not None else {}
        now = time.monotonic()
        bucket = state.setdefault(hook.id, deque(maxlen=max(1, hook.rate_per_min)))
        while bucket and now - bucket[0] > 60.0:
            bucket.popleft()
        if len(bucket) >= hook.rate_per_min:
            return HookOutcome(429, "чаще нельзя — rate limit ряда")
        bucket.append(now)
    payload = " ".join(body.decode("utf-8", "replace").split())[:4000]
    await store.bump_fire(hook.id)
    return HookOutcome(200, "принято", hook=hook, payload=payload)


async def serve_hooks(
    *,
    store: Any,
    host: str,
    port: int,
    max_body: int = _MAX_BODY_DEFAULT,
    on_fire: Callable[[HookRow, str], Any] | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Цикл сервера: читает один запрос на соединение, отвечает и закрывает (keep-alive не жмём).

    ``on_fire(hook, payload)`` — callback бота: он решает notify/turn и доставляет. Вызывается
    фоново (create_task), чтобы HTTP-клиент получил 200 сразу, а не после модельного хода.
    """
    rate_state: dict[str, deque[float]] = defaultdict(deque)
    background: set[asyncio.Task[Any]] = set()

    async def _client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            request_line = lines[0].split(" ")
            if len(request_line) < 2:
                raise ValueError("кривая строка запроса")
            method, path = request_line[0], request_line[1]
            headers = {}
            for ln in lines[1:]:
                if ":" in ln:
                    k, _, v = ln.partition(":")
                    headers[k.strip().lower()] = v.strip()
            try:
                length = min(int(headers.get("content-length", "0")), max_body + 1)
            except ValueError:
                length = max_body + 1
            body = await reader.readexactly(length) if length > 0 else b""
            outcome = await handle_hook_request(
                store=store,
                method=method,
                path=path,
                headers=headers,
                body=body,
                max_body=max_body,
                rate_state=rate_state,
            )
            if outcome.hook is not None and outcome.fired and on_fire is not None:
                hook, payload = outcome.hook, outcome.payload
                task = asyncio.create_task(_fire(on_fire, hook, payload), name="hook-fire")
                background.add(task)
                task.add_done_callback(background.discard)
            reply = outcome.text.encode("utf-8")
            writer.write(
                b"HTTP/1.1 "
                + str(outcome.status).encode()
                + b" "
                + outcome.text.encode()
                + b"\r\ncontent-type: text/plain; charset=utf-8\r\ncontent-length: "
                + str(len(reply)).encode()
                + b"\r\nconnection: close\r\n\r\n"
                + reply
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ValueError, ConnectionResetError):
            writer.write(
                b"HTTP/1.1 400 bad request\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"
            )
            with suppress(Exception):
                await writer.drain()
        finally:
            with suppress(Exception):
                writer.close()

    server = await asyncio.start_server(_client, host=host, port=port)
    log.info("hooks.serving", host=host, port=port)
    try:
        if stop_event is not None:
            await stop_event.wait()
        else:
            await server.serve_forever()
    finally:
        server.close()
        await server.wait_closed()
        for task in list(background):
            task.cancel()


async def _fire(on_fire: Callable[[HookRow, str], Any], hook: HookRow, payload: str) -> None:
    try:
        await on_fire(hook, payload)
    except Exception:  # noqa: BLE001 — огонь на стороне бота не должен ронять приёмник
        log.exception("hooks.fire_failed", hook=hook.name)
