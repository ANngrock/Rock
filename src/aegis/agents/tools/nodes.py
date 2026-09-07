"""Инструменты узлов: «сделай на компьютере» с риском по природе действия.

Риск-лестница выбрана не «для красоты»: уведомления не требуют подтверждения (безобидны),
скриншот — требует (он уносит приватное изображение с машины), произвольная команда — требует
всегда (HIGH). Модель не может «понизить» риск выбором action: для run это отдельный
инструмент, а не аргумент — иначе «просто проверь систему» эскалировалось бы одной правкой args.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from aegis.agents.tools.registry import ToolContext, registry
from aegis.governance.policy import Risk
from aegis.planning.nodes import SqlNodeStore
from aegis.platform.config import settings

__all__ = ["node_notify", "node_run", "node_screenshot", "node_status"]


def _guard() -> str | None:
    cfg = settings()
    if not cfg.nodes_enabled:
        return "Узлы выключены (NODES_ENABLED=false) — машинами никто не управляет."
    return None


async def _enqueue(node_ref: str, action: str, payload: dict[str, Any], ctx: ToolContext) -> str:
    cfg = settings()
    store = SqlNodeStore()
    node = await store.find(owner_id=ctx.owner_id, ref=node_ref)
    if node is None:
        return f"Узел «{node_ref}» не найден среди привязанных — aegis node enroll сначала."
    if node.status != "paired":
        return f"Узел «{node.name}» в статусе {node.status}: он не исполнит команду."
    cmd_id = await store.enqueue(
        node=node,
        owner_id=ctx.owner_id,
        action=action,
        payload=payload,
        trace_id=ctx.trace_id,
        ttl_seconds=cfg.node_cmd_ttl_seconds,
    )
    return (
        f"Команда «{action}» поставлена узлу {node.name} (id={cmd_id[:8]}); "
        "результат придёт владельцу отдельным сообщением, когда узел его вернёт."
    )


class NoArgs(BaseModel):
    """Пустой набор аргументов (для OpenAI-схемы важен ``type: object``)."""


class NodeRefArgs(BaseModel):
    node: str = Field(
        min_length=2,
        max_length=64,
        description="имя узла или начало его id (узел обязан быть привязан заранее)",
    )


class NotifyArgs(NodeRefArgs):
    text: str = Field(min_length=1, max_length=500, description="что показать на рабочем столе")


class RunArgs(NodeRefArgs):
    command: str = Field(
        min_length=1,
        max_length=2000,
        description="команда оболочки на машине владельца (её увидит и подтвердит владелец)",
    )
    timeout_s: int = Field(default=60, ge=1, le=300)


@registry.register(
    "node_status",
    "Состояние узлов владельца: статус связки, последний heartbeat, снапшот ОС. Только чтение.",
    NoArgs,
)
async def node_status(args: Any, ctx: ToolContext) -> str:
    blocked = _guard()
    if blocked:
        return blocked
    store = SqlNodeStore()
    nodes = await store.list_nodes(owner_id=ctx.owner_id)
    if not nodes:
        return "Узлов нет. Привязка: aegis node enroll ИМЯ на сервере, затем демон на машине."
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    lines = []
    for n in nodes:
        seen = "никогда"
        if n.last_seen is not None:
            age = int((now - n.last_seen).total_seconds())
            seen = f"{age} с назад"
        lines.append(f"{n.name}: {n.status}, heartbeat {seen}, os={n.caps.get('os', '?')}")
    return "\n".join(lines)


@registry.register(
    "node_notify",
    "Показать уведомление на рабочем столе узла (без подтверждения: безвредно).",
    NotifyArgs,
    writes=True,
    risk=Risk.LOW,
)
async def node_notify(args: NotifyArgs, ctx: ToolContext) -> str:
    blocked = _guard()
    if blocked:
        return blocked
    return await _enqueue(args.node, "notify", {"text": args.text}, ctx)


@registry.register(
    "node_screenshot",
    "Скриншот рабочего стола узла — приватное изображение с машины; придёт владельцу в чат.",
    NodeRefArgs,
    writes=True,
    risk=Risk.MEDIUM,
)
async def node_screenshot(args: NodeRefArgs, ctx: ToolContext) -> str:
    blocked = _guard()
    if blocked:
        return blocked
    return await _enqueue(args.node, "screenshot", {}, ctx)


@registry.register(
    "node_run",
    "Выполнить команду оболочки на машине владельца. Всегда требует подтверждения владельца.",
    RunArgs,
    writes=True,
    risk=Risk.HIGH,
)
async def node_run(args: RunArgs, ctx: ToolContext) -> str:
    blocked = _guard()
    if blocked:
        return blocked
    return await _enqueue(
        args.node, "run", {"command": args.command, "timeout_s": args.timeout_s}, ctx
    )
