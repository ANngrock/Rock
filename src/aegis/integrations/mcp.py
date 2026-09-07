"""MCP по stdio: минимальный JSON-RPC клиент на newline-delimited фреймах.

Почему сами, а не пакет `mcp`: протокол для наших нужд — initialize + tools/list + tools/call,
плюс ровно один процесс-ребёнок на коннектор. Зависимость на полный SDK тянула бы транспорт,
который мы не используем, а «библиотека на 4000 строк ради трёх методов» — это не бережливость.

Фрейминг: спецификация MCP для stdio — JSON-RPC сообщения, разделённые переводом строки. Всё,
что приходит ДО ответа на запрос (уведомления server->client), корректно пропускаются; stdout
сервера — единственный канал протокола, stderr только в причину падения.

Что здесь принципиально:
* НИКАКИХ долгих ожиданий без таймаута: зависший сервер = ошибка пробы, а не зависший бот;
* процесс убивается при любой ошибке (wait + kill), а не остаётся «висеть на трубке»;
* вывод сервера — untrusted данные; здесь мы их даже не интерпретируем, кроме как content blocks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

__all__ = ["McpClient", "McpError"]

_PROTOCOL_VERSION = "2024-11-05"


class McpError(RuntimeError):
    """Любой сбой протокола/процесса — одним классом: выше это «коннектор нерабочий»."""


class McpClient:
    """Одна сессия = один дочерний процесс. Методы потокобезопасны последовательным локом."""

    def __init__(
        self,
        *,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._command = command
        self._args = list(args or [])
        self._env = dict(env or {})
        self._timeout = float(timeout_s)
        self._proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> McpClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    async def start(self) -> None:
        if self._proc is not None:
            return
        import os

        env = {**os.environ, **self._env}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (OSError, ValueError) as exc:
            raise McpError(f"не запустился {self._command!r}: {str(exc)[:160]}") from exc
        try:
            init = await self._request(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "aegis", "version": "1"},
                },
            )
        except McpError:
            await self.stop()
            raise
        _ = init  # serverInfo пригодится в выводе пробы — оставим его внутри _server_info
        with contextlib.suppress(Exception):
            await self._notify("notifications/initialized", {})

    async def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(Exception):
            if proc.stdin is not None:
                proc.stdin.close()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=2)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            result = await self._request("tools/list", params)
            tools.extend(dict(t) for t in (result.get("tools") or []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """(текст результата, isError). Только text blocks; остальные типы — заглушкой-описанием:
        картинку из MCP мы в чат пока не переносим, и врать, что переносим, не будем."""
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        bits: list[str] = []
        for block in result.get("content") or []:
            block = dict(block)
            if block.get("type") == "text":
                bits.append(str(block.get("text") or ""))
            else:
                bits.append(f"[{block.get('type', 'блок')} пропущен]")
        return "\n".join(bits)[:20_000], bool(result.get("isError"))

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            self._id += 1
            rid = self._id
            await self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            while True:
                msg = await self._read()
                if msg.get("id") == rid:
                    if "error" in msg:
                        err = msg["error"]
                        raise McpError(
                            f"{method}: {err.get('message', err) if isinstance(err, dict) else err}"
                        )
                    result = msg.get("result")
                    if not isinstance(result, dict):
                        raise McpError(f"{method}: result не объект")
                    return result
                # чужой id/уведомление — пропускаем: порядок сервера не наш вопрос

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        async with self._lock:
            await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, msg: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpError("сессия не запущена")
        try:
            proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError, ValueError) as exc:
            raise McpError(f"сервер закрыл stdin: {type(exc).__name__}") from exc

    async def _read(self) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise McpError("сессия не запущена")
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=self._timeout)
        except TimeoutError as exc:
            raise McpError(f"таймаут {self._timeout:.0f}s: сервер не ответил") from exc
        if not line:
            tail = ""
            if proc.stderr is not None:
                with contextlib.suppress(Exception):
                    tail = (await proc.stderr.read(1000)).decode("utf-8", "replace")[-400:]
            raise McpError(f"сервер закрыл stdout{': ' + tail.strip() if tail.strip() else ''}")
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            raise McpError(f"не-JSON в stdout: {line[:120]!r}") from exc
        if not isinstance(msg, dict):
            raise McpError("не-объект в stdout")
        return msg
