"""Мост «коннектор → реестр инструментов».

Внешний инструмент входит в систему ОДИНАКОВО со всех сторон: trust=untrusted, риск из
аннотаций сервера (readOnly → LOW, всё остальное минимум MEDIUM — то есть подтверждение в чате),
схема аргументов передаётся дословно. «MCP-сервер сказал» не является основанием исполнить
запись: это тот же принцип, что и с веб-страницами (принцип 2), просто у страницы не было
красивого протокола.

Один вызов = один spawned-процесс. Персистентные сессии оставили бы в боте детей-зомби и
«состояние прошлого разговора» на сервере, который обязан быть государственным; лишняя
миллисекунда запуска дешевле неубираемого трупа.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from pydantic import BaseModel, Field

from aegis.agents.tools.registry import ToolContext, ToolResult, registry
from aegis.governance.policy import Risk
from aegis.integrations.mcp import McpClient, McpError
from aegis.integrations.store import Connector, SqlConnectorStore

__all__ = ["load_connectors_into_registry"]

log = structlog.get_logger(__name__)


class McpToolArgs(BaseModel):
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="аргументы инструмента — объект по схеме из описания инструмента",
    )


def _risk_of(tool_def: dict[str, Any]) -> tuple[Risk, bool]:
    ann = tool_def.get("annotations") or {}
    if ann.get("destructiveHint") is True or ann.get("openWorldHint") is True:
        return Risk.HIGH, True
    if ann.get("readOnlyHint") is True:
        return Risk.LOW, False
    # без честной аннотации доверяем меньше: неизвестное действие = подтверждение
    return Risk.MEDIUM, True


def _schema_hint(input_schema: dict[str, Any]) -> str:
    props = input_schema.get("properties") or {}
    required = list(input_schema.get("required") or [])
    if not props:
        return "аргументов не принимает"
    parts = []
    for key, spec in list(props.items())[:20]:
        tname = str((spec or {}).get("type", "any"))
        mark = "*" if key in required else ""
        desc = str((spec or {}).get("description") or "")[:80]
        parts.append(f"{key}{mark} ({tname}){' — ' + desc if desc else ''}")
    return "аргументы: " + "; ".join(parts) + (" (* — обязательно)" if required else "")


def _sanitize(connector_name: str, tool_name: str) -> str:
    raw = f"mcp__{connector_name}__{tool_name}"
    cleaned = "".join(c if (c.isalnum() or c in "_") else "_" for c in raw)
    return cleaned[:64]


async def load_connectors_into_registry(
    cfg: Any, store: SqlConnectorStore | None = None
) -> dict[str, Any]:
    """Зарегистрировать инструменты включённых коннекторов. Никогда не бросает: бот без
    подключений жив (принцип 5), а «почему не подключилось» видно в возврате и логе."""
    out: dict[str, Any] = {"registered": 0, "connectors": 0, "notes": []}
    if not cfg.integrations_enabled:
        out["notes"].append("INTEGRATIONS_ENABLED=false — подключения не загружены")
        return out
    store = store or SqlConnectorStore()
    try:
        connectors = await store.list(owner_id=int(cfg.telegram_owner_id or 1), enabled_only=True)
    except Exception as exc:  # noqa: BLE001 - без БД просто нет реестра, не катастрофа
        out["notes"].append(f"реестр подключений недоступен: {type(exc).__name__}")
        return out
    for conn in connectors:
        try:
            if conn.kind == "mcp":
                n = await _register_mcp(conn, cfg, store, out)
            elif conn.kind == "plugin":
                n = _register_plugin(conn, out)
            else:  # api: инструмента нет — это секрет для будущих built-in-сервисов
                n = 0
                out["connectors"] += 1
                continue
            out["connectors"] += 1
            out["registered"] += n
            if n:
                await store.record_probe(connector_id=conn.id, ok=True)
        except Exception as exc:  # noqa: BLE001 - один кривой коннектор не заводит остальные
            out["notes"].append(f"{conn.kind}/{conn.name}: {str(exc)[:200]}")
            out["registered_skipped"] = int(out.get("registered_skipped", 0)) + 1
            log.warning("integrations.connector_failed", name=conn.name, err=repr(exc)[:200])
            try:
                await store.record_probe(connector_id=conn.id, ok=False, error=str(exc)[:400])
            except Exception:  # noqa: BLE001, S110 - запись диагноза не важнее диагноза в логе
                pass
    return out


async def mcp_launch_params(conn: Connector, store: SqlConnectorStore) -> dict[str, Any]:
    """command/args/env для spawn — один путь у бота и у `aegis connect probe`: проба обязана
    проверять ровно то, на чём бот потом работает, иначе «probe зелёный, в бою падает»."""
    command = str(conn.config.get("command") or "")
    if not command:
        raise McpError("пустой command")
    env = dict(conn.config.get("env") or {})
    secret = await store.secret_for_mcp(owner_id=conn.owner_id, connector=conn)
    secret_env = str(conn.config.get("secret_env") or "")
    if secret_env:
        if secret is None:
            raise McpError(f"секрет для env.{secret_env} не задан (aegis connect secret)")
        env[secret_env] = secret
    elif conn.config.get("requires_secret") and secret is None:
        raise McpError("сервер требует секрет, но он не задан")
    return {"command": command, "args": list(conn.config.get("args") or []), "env": env}


async def _register_mcp(
    conn: Connector, cfg: Any, store: SqlConnectorStore, out: dict[str, Any]
) -> int:
    timeout = float(cfg.mcp_call_timeout_seconds)
    launch = await mcp_launch_params(conn, store)

    async def _session() -> McpClient:
        client = McpClient(**launch, timeout_s=timeout)
        await client.start()
        return client

    # tools/list — один раз при загрузке; живой сервер отвечает за список, кривой — за ошибку
    probe = McpClient(**launch, timeout_s=timeout)
    try:
        await probe.start()
        tools = await probe.list_tools()
    finally:
        await probe.stop()
    count = 0
    for tool in tools[: int(cfg.mcp_max_tools)]:
        name = str(tool.get("name") or "").strip()
        if not name or count >= int(cfg.mcp_max_tools):
            continue
        reg_name = _sanitize(conn.name, name)
        if reg_name in registry._tools:  # noqa: SLF001 - переопределять чужое имя запрещено реестром
            continue
        risk, writes = _risk_of(tool)
        desc = f"[MCP {conn.name}] {str(tool.get('description') or '').strip()[:400]}"
        hint = _schema_hint(dict(tool.get("inputSchema") or {}))
        tool_name = name

        async def handler(args: McpToolArgs, ctx: ToolContext, _tn: str = tool_name) -> ToolResult:
            client = await _session()
            try:
                text, is_error = await client.call_tool(_tn, dict(args.arguments))
            finally:
                await client.stop()
            prefix = "MCP вернул ошибку: " if is_error else ""
            return ToolResult(prefix + text, trust="untrusted")

        registry.register(reg_name, f"{desc}. {hint}", McpToolArgs, writes=writes, risk=risk)(
            handler
        )
        count += 1
    return count


def _register_plugin(conn: Connector, out: dict[str, Any]) -> int:
    module_path = str(conn.config.get("module") or "")
    import importlib

    try:
        module = importlib.import_module(module_path)
    except Exception as exc:  # noqa: BLE001
        raise McpError(f"плагин {module_path} не импортируется: {type(exc).__name__}") from exc
    register = getattr(module, "register", None)
    if not callable(register):
        raise McpError(f"в плагине {module_path} нет register(registry)")
    before = set(registry.names())
    register(registry)
    return len(set(registry.names()) - before)


def describe_connectors(connectors: list[Connector]) -> str:
    lines = []
    for c in connectors:
        mark = "►" if c.enabled else "×"
        err = f" ! {str(c.last_error)[:100]}" if c.last_error else ""
        ok = f" ok {c.last_ok_at:%d.%m %H:%M}" if c.last_ok_at else ""
        lines.append(f" {mark} {c.kind}/{c.name}{ok}{err}")
    return "\n".join(lines) or "подключений нет"


def config_digest(config: dict[str, Any]) -> str:
    return json.dumps(config, ensure_ascii=False, sort_keys=True)[:400]
