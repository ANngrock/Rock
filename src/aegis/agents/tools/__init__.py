"""Инструменты агента: реестр + встроенные обёртки над use cases доменов."""

from __future__ import annotations

from aegis.agents.tools.registry import ToolContext, ToolRegistry, registry

__all__ = ["ToolContext", "ToolRegistry", "load_builtin_tools", "registry"]

_loaded = False


def load_builtin_tools() -> None:
    """Зарегистрировать все инструменты приложения. Повторный вызов — ничего не делает.

    Инструмент регистрируется импортом своего модуля, и именно поэтому список собран в одном месте:
    `bot`, `ask`, `doctor`, `tools` и `repro replay` перечисляли модули сами, из-за чего новый
    инструмент можно было «забыть» в одной из точек — а в тестах такого не видно: там реестр
    собирается свой. «Напоминания работают в чате, но `aegis ask` о них не знает» — ровно этот баг.
    """
    global _loaded
    if _loaded:
        return
    from aegis.agents.tools import builtin, reminders, repro  # noqa: F401

    _loaded = True
