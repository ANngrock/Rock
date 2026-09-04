"""Аудит: что именно делал агент, на какой модели, сколько это стоило и что решил policy.

Два потока записей (обе таблицы — append-only, см. миграцию 0001):

* ``platform.llm_calls``  — каждый attempt вызова модели, включая провальные;
* ``governance.tool_runs`` — каждый вызов инструмента с решением policy.

Aдаптеры отделены портом :class:`AuditLog`, чтобы supervisor не зависел от наличия БД:
без БД работает :class:`NullAudit`, и бот деградирует, а не падает (принцип 5).
"""

from __future__ import annotations

from typing import Any, Protocol

import orjson
import structlog
from sqlalchemy import text

from aegis.platform.db import session
from aegis.platform.gateway.client import LLMCallRecord

__all__ = ["AuditLog", "NullAudit", "SqlAuditLog", "record_llm_call", "record_tool_run"]

log = structlog.get_logger(__name__)

_RESULT_CAP = 8000


class AuditLog(Protocol):
    async def llm_call(self, record: LLMCallRecord) -> None: ...

    async def tool_run(
        self,
        *,
        trace_id: str,
        tool: str,
        args: dict[str, Any],
        decision: str,
        result: str | None,
        ok: bool,
        owner_id: int,
    ) -> None: ...


class NullAudit:
    """Когда БД недоступна: пишем в лог и живём дальше."""

    async def llm_call(self, record: LLMCallRecord) -> None:
        if not record.ok:
            log.warning(
                "llm.call_failed", model=record.model, err=record.error, trace_id=record.trace_id
            )

    async def tool_run(
        self,
        *,
        trace_id: str,
        tool: str,
        args: dict[str, Any],
        decision: str,
        result: str | None,
        ok: bool,
        owner_id: int,
    ) -> None:
        log.info("tool.run", tool=tool, decision=decision, ok=ok, trace_id=trace_id)


class SqlAuditLog:
    """Запись в ``platform.llm_calls`` / ``governance.tool_runs``.

    Сбой БД не поднимается выше: аудит не имеет права отнимать у владельца ответ. Но он и не
    молчит — :attr:`failures` виден в ``/status`` и в doctor, потому что потерянная трасса
    означает потерянную воспроизводимость (SLO шага 1), а не «всё немного хуже».
    """

    def __init__(self) -> None:
        self.failures = 0

    @property
    def degraded(self) -> bool:
        return self.failures > 0

    async def llm_call(self, record: LLMCallRecord) -> None:
        try:
            async with session() as s:
                await s.execute(
                    text(
                        """
                        INSERT INTO platform.llm_calls
                            (call_id, role, model, trace_id, provider, attempt,
                             prompt_tokens, completion_tokens, cost_usd, latency_ms, ok, error)
                        VALUES (CAST(:call_id AS uuid), :role, :model, CAST(:trace_id AS uuid),
                                :provider, :attempt, :prompt_tokens, :completion_tokens,
                                :cost_usd, :latency_ms, :ok, :error)
                        """
                    ).bindparams(
                        call_id=record.call_id,
                        role=record.role,
                        model=record.model,
                        trace_id=record.trace_id,
                        provider=record.provider,
                        attempt=record.attempt,
                        prompt_tokens=record.prompt_tokens,
                        completion_tokens=record.completion_tokens,
                        cost_usd=record.cost_usd,
                        latency_ms=record.latency_ms,
                        ok=record.ok,
                        error=record.error,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - аудит не должен ронять пользовательский запрос
            self.failures += 1
            log.warning(
                "audit.llm_call_failed",
                failures=self.failures,
                err=repr(exc)[:300],
                trace_id=record.trace_id,
            )

    async def tool_run(
        self,
        *,
        trace_id: str,
        tool: str,
        args: dict[str, Any],
        decision: str,
        result: str | None,
        ok: bool,
        owner_id: int,
    ) -> None:
        try:
            async with session() as s:
                await s.execute(
                    text(
                        """
                        INSERT INTO governance.tool_runs
                            (trace_id, owner_id, tool, args, decision, result, ok)
                        VALUES (CAST(:trace_id AS uuid), :owner_id, :tool,
                                CAST(:args AS jsonb), :decision, :result, :ok)
                        """
                    ).bindparams(
                        trace_id=trace_id,
                        owner_id=owner_id,
                        tool=tool,
                        args=orjson.dumps(args).decode(),
                        decision=decision,
                        result=(result or "")[:_RESULT_CAP],
                        ok=ok,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            log.warning(
                "audit.tool_run_failed",
                failures=self.failures,
                err=repr(exc)[:300],
                trace_id=trace_id,
                tool=tool,
            )


# --- функциональные обёртки: исторический API, удобный для DI в aiogram-хендлерах ---


async def record_llm_call(record: LLMCallRecord) -> None:
    await SqlAuditLog().llm_call(record)


async def record_tool_run(
    trace_id: str,
    tool: str,
    args: dict[str, Any],
    decision: str,
    result: str | None,
    ok: bool,
    owner_id: int = 0,
) -> None:
    await SqlAuditLog().tool_run(
        trace_id=trace_id,
        tool=tool,
        args=args,
        decision=decision,
        result=result,
        ok=ok,
        owner_id=owner_id,
    )
