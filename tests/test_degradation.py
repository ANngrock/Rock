"""Деградация трассировки: сломанный event sink/audit не смеет ломать ответ владельцу.

Ровно этот инцидент и закрыт: при отсутствии таблиц BestEffortEventSink ловил исключение и
писал `log.warning(..., event=...)`; `event` — зарезервированный ключ structlog (текст
сообщения), поэтому логгер бросал `TypeError: got multiple values for argument 'event'`,
и «безопасная деградация» роняла каждый ход. Тесты на фейках этого не видели.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest

from aegis.platform.events.sink import BestEffortEventSink

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "aegis"


class _Boom(Exception):
    pass


class _RaisingSink:
    def __init__(self, message: str = 'relation "platform.events" does not exist') -> None:
        self.message = message
        self.calls = 0

    async def append(self, **kwargs: Any) -> None:  # noqa: ANN401, ARG002
        self.calls += 1
        raise _Boom(self.message)


class _RecordingSink:
    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def append(self, **kwargs: Any) -> None:  # noqa: ANN401, ARG002
        self.seen.append(kwargs)


async def test_missing_relation_is_swallowed() -> None:
    inner = _RaisingSink()
    sink = BestEffortEventSink(inner)
    await sink.append(stream_type="owner", stream_id="owner:1", event_type="message.received")
    assert inner.calls == 1
    assert sink.degraded is True
    assert sink.failures == 1


async def test_hint_emitted_once_and_logging_itself_does_not_raise(caplog) -> None:  # noqa: ANN001
    inner = _RaisingSink()
    sink = BestEffortEventSink(inner)
    for _ in range(50):
        await sink.append(stream_type="owner", stream_id="owner:1", event_type="message.received")
    assert inner.calls == 50
    assert sink.failures == 50
    assert sink._hinted_missing_schema is True


async def test_other_errors_do_not_trigger_schema_hint() -> None:
    sink = BestEffortEventSink(_RaisingSink("connection refused"))
    await sink.append(stream_type="owner", stream_id="owner:1", event_type="x")
    assert sink._hinted_missing_schema is False
    assert sink.degraded is True


async def test_success_after_failure_clears_nothing_but_keeps_working() -> None:
    inner = _RecordingSink()
    sink = BestEffortEventSink(inner)
    await sink.append(stream_type="owner", stream_id="owner:1", event_type="x")
    assert sink.degraded is False
    assert inner.seen[0]["event_type"] == "x"


# ---------------------------------------------------------------- статическая защита


def _log_calls_with_reserved_kw(tree: ast.AST) -> list[str]:
    bad: list[str] = []
    reserved = {"event"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"debug", "info", "warning", "error", "exception", "critical"}:
            continue
        value = node.func.value
        name = getattr(value, "id", None) or getattr(value, "attr", None)
        if name not in {"log", "logger"}:
            continue
        for kw in node.keywords:
            if kw.arg in reserved:
                bad.append(f"{ast.unparse(node)}")
    return bad


@pytest.mark.parametrize(
    "path", sorted(SRC.rglob("*.py")), ids=lambda p: str(p.relative_to(SRC.parent))
)
def test_no_structlog_reserved_kwargs_in_source(path: pathlib.Path) -> None:
    """Ни одного `log.*(event=...)` в исходниках: это TypeError внутри обработки отказа."""
    offenders = _log_calls_with_reserved_kw(ast.parse(path.read_text(), filename=str(path)))
    assert not offenders, f"{path.name}: зарезервированный ключ structlog -> {offenders}"
