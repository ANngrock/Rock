"""structlog: JSON в проде, человекочитаемо в dev.

trace_id/owner_id биндятся через contextvars (`bind_contextvars`) — каждое лог-событие в
пределах обработки сообщения несёт трассу, без пробрасывания аргументов сквозь весь стек.
"""

from __future__ import annotations

import logging
import sys
from typing import cast

import structlog
from structlog.typing import Processor

__all__ = ["bind_contextvars", "get_logger", "setup_logging"]

_configured = False


def _level(name: str) -> int:
    resolved = logging.getLevelName(name.upper())
    return int(resolved) if isinstance(resolved, int) else logging.INFO


def setup_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    global _configured
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
    ]
    tail: list[Processor]
    if json_output:
        tail = [
            structlog.processors.TimeStamper(fmt="iso", key="ts"),
            structlog.processors.JSONRenderer(),
        ]
    else:
        tail = [
            structlog.processors.TimeStamper(fmt="%H:%M:%S", key="ts", utc=False),
            structlog.dev.ConsoleRenderer(colors=False),
        ]
    # логи — в stderr: у CLI есть команды с машинным выводом на stdout (aegis doctor --json | jq)
    logging.basicConfig(level=_level(level), format="%(message)s", stream=sys.stderr, force=True)
    structlog.configure(
        processors=[*shared, *tail],
        wrapper_class=structlog.make_filtering_bound_logger(_level(level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    if not _configured:  # импорт в тестах/скриптах без явной инициализации
        setup_logging(json_output=False)
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))


def bind_contextvars(**kw: object) -> None:
    structlog.contextvars.bind_contextvars(**kw)
