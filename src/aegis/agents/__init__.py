"""Агенты: supervisor, реестр инструментов, промпты.

Иерархия зависимостей одного направления: ``interaction → agents → (domains | platform)``.
Инструменты — тонкие обёртки над use cases доменов; никакой бизнес-логики внутри handler'ов.
"""

from __future__ import annotations

from aegis.agents.supervisor import Inbound, PendingAction, Reply, Supervisor

__all__ = ["Inbound", "PendingAction", "Reply", "Supervisor"]
