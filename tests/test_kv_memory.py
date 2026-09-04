"""Память-бэкенд KV и поведение суперайзера при падении Redis.

Проверка принципа 5 («деградация — не катастрофа») в том месте, где она обычно и ломается:
кэш сессий лёг, а владелец продолжает получать ответы — и никакое запись при этом не
выполняется «втихую».
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from aegis.agents.services import Services
from aegis.agents.supervisor import Inbound, Supervisor
from aegis.agents.tools.registry import ToolContext, ToolRegistry
from aegis.governance.policy import PolicyEngine, Risk
from aegis.platform.config import Settings
from aegis.platform.gateway.cost import CostGovernor
from aegis.platform.kv_memory import MemoryKV
from conftest import FakeFacts, FakeGateway, FakeNotes, make_chat_result


# ------------------------------------------------------------------ MemoryKV
async def test_set_get_delete_roundtrip() -> None:
    kv = MemoryKV()
    await kv.set("a", b"1")
    assert await kv.get("a") == b"1"
    await kv.set("b", "текст")
    assert await kv.get("b") == "текст".encode()
    assert await kv.delete("a", "нет-такого") == 1
    assert await kv.get("a") is None


async def test_getdel_returns_value_once() -> None:
    kv = MemoryKV()
    await kv.set("pending:x", b"{}")
    assert await kv.getdel("pending:x") == b"{}"
    assert await kv.getdel("pending:x") is None


async def test_incrbyfloat_accumulates() -> None:
    kv = MemoryKV()
    assert await kv.incrbyfloat("cost:day", 0.25) == pytest.approx(0.25)
    assert await kv.incrbyfloat("cost:day", 0.5) == pytest.approx(0.75)


async def test_expire_marks_ttl_and_absent_key() -> None:
    kv = MemoryKV()
    await kv.set("k", "v", ex=60)
    ttl = kv.ttl("k")
    assert ttl is not None and ttl > 50
    assert await kv.expire("nope", 10) is False
    assert kv.keys() == ["k"]
    await kv.aclose()
    assert kv.keys() == []


async def test_cost_governor_works_on_memory_kv() -> None:
    """Счётчик бюджета обязан работать и на памяти — иначе демо-режим врёт про расходы."""
    kv = MemoryKV()
    gov = CostGovernor(kv, daily_limit_usd=1.0)
    assert await gov.spent() == 0.0
    await gov.record(0.4)
    assert await gov.spent() == pytest.approx(0.4)


# ------------------------------------------------- Supervisor при отказе кэша
class BrokenKV:
    """Redis «как после kill -9»: любой вызов — исключение."""

    def __init__(self) -> None:
        self.calls = 0

    async def get(self, name: str) -> bytes | None:
        self._hit()

    async def set(self, name: str, value: object, *, ex: int | None = None) -> bool:
        self._hit()

    async def delete(self, *names: str) -> int:
        self._hit()

    async def getdel(self, name: str) -> bytes | None:
        self._hit()

    def _hit(self) -> None:
        self.calls += 1
        raise ConnectionError("Error connecting to localhost:6379.")


class WriteToolArgs(BaseModel):
    note: str = "заметка"


def make_supervisor(
    kv: Any,
    responses: list[Any],
    *,
    registry: ToolRegistry | None = None,
) -> Supervisor:
    cfg = Settings(_env_file=None, _env_prefix="KVTEST_", max_iterations=3, pending_ttl_seconds=60)
    cost = CostGovernor(kv, daily_limit_usd=1.0)
    gateway = FakeGateway(responses, cost)
    services = Services(
        gateway=gateway,
        facts=FakeFacts(["не пьёт кофе после 16"]),
        notes=FakeNotes(),
    )
    return Supervisor(
        services=services,
        registry=registry or ToolRegistry(),
        policy=PolicyEngine(),
        kv=kv,
        cfg=cfg,
    )


def registry_with_writer(executed: list[str]) -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register("add_note", "заметка", WriteToolArgs, writes=True, risk=Risk.MEDIUM)
    async def add_note(args: WriteToolArgs, ctx: ToolContext) -> str:
        executed.append(args.note)
        return "записано"

    return registry


async def test_reply_survives_broken_kv() -> None:
    """Кэш сессий лёг — ответ всё равно приходит, просто без истории."""
    supervisor = make_supervisor(BrokenKV(), [make_chat_result("держись, я всё равно отвечу")])
    reply = await supervisor.handle(Inbound(text="как дела?", owner_id=1))
    assert "всё равно отвечу" in reply.text
    assert supervisor.kv_degraded is True


async def test_status_reports_kv_degradation() -> None:
    kv = BrokenKV()
    supervisor = make_supervisor(kv, [make_chat_result("ок"), make_chat_result("статус")])
    await supervisor.handle(Inbound(text="привет", owner_id=1))
    status = await supervisor.status(1)
    assert status["kv_degraded"] is True
    assert status["history_messages"] == 0
    assert kv.calls >= 2


async def test_write_is_not_executed_when_pending_cannot_be_stored() -> None:
    """Подтверждение без хранилища невыполнимо: честный отказ вместо «висящей кнопки»."""
    executed: list[str] = []
    responses = [
        make_chat_result(
            "сейчас запишу", tool_calls=[("call_1", "add_note", {"note": "купить билет"})]
        )
    ]
    supervisor = make_supervisor(BrokenKV(), responses, registry=registry_with_writer(executed))
    reply = await supervisor.handle(Inbound(text="запиши заметку", owner_id=1))

    assert executed == [], "запись не имеет права выполниться без подтверждения"
    assert reply.degraded is True
    assert not reply.pending, "нельзя обещать кнопку, за которой нечего хранить"
    assert "не могу" in reply.text.lower() or "не выполнено" in reply.text.lower()
