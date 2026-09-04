"""Supervisor: роутинг, цикл инструментов, подтверждения, деградация.

Это главный контракт шага 1: интерфейс может меняться, а эти инварианты — нет.
"""

from __future__ import annotations

from typing import Any

import orjson
import pytest
from pydantic import BaseModel

from aegis.agents.services import Services
from aegis.agents.supervisor import Inbound, Supervisor
from aegis.agents.tools.registry import Attachment, ToolContext, ToolRegistry
from aegis.governance.killswitch import KillSwitchState
from aegis.governance.policy import PolicyEngine, Risk
from aegis.memory.facts import Fact
from aegis.platform.config import Settings
from aegis.platform.events.sink import InMemoryEventSink
from aegis.platform.gateway.client import ChatResult, ModelUnavailable
from aegis.platform.gateway.cost import BudgetExceeded, CostGovernor
from aegis.platform.kv import history_key
from conftest import FakeFacts, FakeGateway, FakeNotes, make_chat_result


class Args(BaseModel):
    value: str = ""


class FakeKV:
    """Минимальный двойник Redis: get/set/delete/getdel + то, что нужно CostGovernor'у."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    async def set(self, key: str, value: Any, *, ex: int | None = None) -> None:
        self.data[key] = value if isinstance(value, bytes) else str(value).encode()

    async def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.data.pop(k, None) is not None)

    async def getdel(self, key: str) -> bytes | None:
        return self.data.pop(key, None)

    async def incrbyfloat(self, key: str, amount: float) -> float:
        current = float(self.data.get(key, b"0") or 0)
        total = current + amount
        self.data[key] = str(total).encode()
        return total

    async def expire(self, key: str, seconds: int) -> bool:
        return True


class FakeKillSwitch:
    def __init__(self, active: bool = False) -> None:
        self.active = active

    async def is_active(self) -> bool:
        return self.active

    async def state(self) -> KillSwitchState:
        return KillSwitchState(active=self.active, reason="тест")


class Recorder:
    """Шпионит за вызовами инструментов, чтобы проверять «исполнилось / не исполнилось»."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def tool(
        self,
        registry: ToolRegistry,
        name: str,
        *,
        writes: bool = False,
        risk: Risk = Risk.NONE,
        result: str = "готово",
        fail: Exception | None = None,
    ) -> None:
        async def handler(args: Args, ctx: ToolContext) -> str:
            self.calls.append((name, args.model_dump()))
            if fail is not None:
                raise fail
            return result

        registry.register(name, f"тестовый инструмент {name}", Args, writes=writes, risk=risk)(
            handler
        )

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class Harness:
    def __init__(self, responses: list[ChatResult | Exception], **kwargs: Any) -> None:
        self.cfg = kwargs.pop("cfg", None) or Settings(
            max_iterations=4, pending_ttl_seconds=60, history_limit=6
        )
        self.kv = FakeKV()
        self.cost = CostGovernor(self.kv, daily_limit_usd=1.0)
        self.gateway = FakeGateway(responses, self.cost)
        self.facts = kwargs.pop("facts", None) or FakeFacts(["не пьёт кофе после 16"])
        self.services = Services(
            gateway=self.gateway,  # type: ignore[arg-type]
            facts=self.facts,  # type: ignore[arg-type]
            notes=kwargs.pop("notes", None) or FakeNotes(),  # type: ignore[arg-type]
        )
        self.registry = ToolRegistry()
        self.recorder = Recorder()
        self.events = InMemoryEventSink()
        self.kill_switch = FakeKillSwitch(kwargs.pop("kill_active", False))
        self.supervisor = Supervisor(
            services=self.services,
            registry=self.registry,
            policy=PolicyEngine(auto_allow_low_risk=kwargs.pop("auto_allow_low_risk", True)),
            kv=self.kv,  # type: ignore[arg-type]
            cfg=self.cfg,
            events=self.events,
            kill_switch=self.kill_switch,  # type: ignore[arg-type]
        )

    def tool(self, name: str, **kwargs: Any) -> None:
        self.recorder.tool(self.registry, name, **kwargs)

    async def handle(self, text: str = "запрос", owner_id: int = 1, **kwargs: Any) -> Any:
        return await self.supervisor.handle(Inbound(text=text, owner_id=owner_id, **kwargs))

    async def resume(self, pending_id: str, approved: bool, owner_id: int = 1) -> Any:
        return await self.supervisor.resume(pending_id, approved, owner_id)

    def history(self, owner_id: int = 1) -> list[dict[str, str]]:
        raw = self.kv.data.get(history_key(owner_id))
        return orjson.loads(raw) if raw else []

    def pending(self) -> dict[str, Any]:
        key = next(k for k in self.kv.data if k.startswith("pending:"))
        return orjson.loads(self.kv.data[key])

    def events_of(self, type_name: str) -> list[dict[str, Any]]:
        return [e for e in self.events.events if e["type"] == type_name]


def harness(responses: list[Any], **kwargs: Any) -> Harness:
    return Harness(responses, **kwargs)


# ------------------------------------------------------------------ роутинг


async def test_smalltalk_goes_to_fast_model_without_tools() -> None:
    h = harness([make_chat_result("Привет!")])
    reply = await h.handle("привет")
    assert reply.text == "Привет!"
    assert h.gateway.calls[0]["role"] == "fast"
    assert h.gateway.calls[0]["tools"] is None


async def test_analysis_request_turns_thinking_on() -> None:
    h = harness([make_chat_result("думал, ответил")])
    await h.handle("проанализируй мои траты за март")
    assert h.gateway.calls[0]["role"] == "brain"
    assert h.gateway.calls[0]["thinking"] is True


async def test_cheap_hint_disables_thinking_even_with_analysis_words() -> None:
    h = harness([make_chat_result("ок")])
    await h.handle("проанализируй коротко")
    assert h.gateway.calls[0]["thinking"] is False


async def test_attachments_force_brain_with_tools() -> None:
    h = harness([make_chat_result("что-то на фото")])
    await h.handle("", attachments=[Attachment(data=b"abc", mime="image/jpeg")])
    assert h.gateway.calls[0]["role"] == "brain"
    assert h.gateway.calls[0]["tools"] is not None


async def test_budget_over_85_percent_degrades_to_fast() -> None:
    h = harness([make_chat_result("экономно")])
    await h.cost.record(0.9)  # 90 % от дневного $1
    await h.handle("обычный рабочий запрос")
    assert h.gateway.calls[0]["role"] == "fast"


# ------------------------------------------------------------------ цикл инструментов


async def test_tool_is_executed_and_result_returned_to_model() -> None:
    h = harness(
        [make_chat_result(None, [("c1", "search", {"value": "кит"})]), make_chat_result("нашёл")]
    )
    h.tool("search")
    reply = await h.handle("найди кита", owner_id=7)

    assert h.recorder.calls == [("search", {"value": "кит"})]
    assert reply.text == "нашёл"
    assert reply.iterations == 2
    tool_messages = [m for m in h.gateway.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages[0]["content"] == "готово"
    assert tool_messages[0]["tool_call_id"] == "c1"
    assert h.events_of("tool.executed")


async def test_unknown_tool_becomes_error_message_not_crash() -> None:
    h = harness([make_chat_result(None, [("cX", "nope", {})]), make_chat_result("инструмента нет")])
    reply = await h.handle("сделай")
    assert reply.text == "инструмента нет"
    tool_msg = [m for m in h.gateway.calls[1]["messages"] if m.get("role") == "tool"][-1]
    assert "ERROR" in tool_msg["content"]


async def test_failing_tool_is_reported_to_model() -> None:
    h = harness(
        [
            make_chat_result(None, [("c1", "boom", {})]),
            make_chat_result("разобрался"),
        ],
    )
    h.tool("boom", fail=RuntimeError("нет соединения"))
    reply = await h.handle("сделай")
    tool_msg = [m for m in h.gateway.calls[1]["messages"] if m.get("role") == "tool"][-1]
    assert tool_msg["content"].startswith("ERROR: RuntimeError")
    assert "нет соединения" in tool_msg["content"]
    assert reply.text == "разобрался"
    assert h.events_of("tool.failed")


async def test_iterations_are_capped() -> None:
    h = harness([make_chat_result(None, [("c", "loop", {})])] * 4)
    h.tool("loop")
    reply = await h.handle("вечный цикл")
    assert reply.degraded is True
    assert "4 шагов" in reply.text


# ------------------------------------------------------------------ подтверждения


async def test_high_risk_write_waits_for_owner() -> None:
    h = harness([make_chat_result(None, [("c1", "pay", {"value": "1000"})])])
    h.tool("pay", writes=True, risk=Risk.HIGH)
    reply = await h.handle("переведи 1000")

    assert h.recorder.calls == [], "действие не должно исполниться без подтверждения"
    assert reply.needs_confirmation
    assert reply.pending[0].tool == "pay"
    assert "pay" in reply.text
    # каждый tool_call обязан получить ответ, иначе следующий запрос к API невалиден
    assert any(
        m.get("role") == "tool" and m.get("tool_call_id") == "c1"
        for m in h.gateway.calls[0]["messages"]
    )
    assert h.events_of("action.confirmation_requested")


async def test_confirm_then_resume_executes_once_and_continues() -> None:
    h = harness(
        [
            make_chat_result(None, [("c1", "pay", {"value": "1000"})]),
            make_chat_result("перевод выполнен"),
        ]
    )
    h.tool("pay", writes=True, risk=Risk.HIGH)
    reply = await h.handle("переведи 1000")

    final = await h.resume(reply.pending_id or "x", True)

    assert h.recorder.calls == [("pay", {"value": "1000"})]
    assert final.text == "перевод выполнен"
    tool_messages = [m for m in h.gateway.calls[-1]["messages"] if m.get("role") == "tool"]
    assert tool_messages[-1]["content"] == "готово", "placeholder должен быть заменён результатом"
    assert all("ожидает подтверждения" not in m["content"] for m in tool_messages)


async def test_deny_does_not_execute_and_answers_shortly() -> None:
    h = harness([make_chat_result(None, [("c1", "pay", {"value": "1"})])])
    h.tool("pay", writes=True, risk=Risk.HIGH)
    reply = await h.handle("переведи")
    final = await h.resume(reply.pending_id or "x", False)

    assert h.recorder.calls == []
    assert "Отменено" in final.text
    resolved = h.events_of("action.confirmation_resolved")
    assert resolved and resolved[0]["payload"]["approved"] is False


async def test_pending_can_be_resolved_only_once() -> None:
    h = harness(
        [make_chat_result(None, [("c1", "pay", {"value": "1"})]), make_chat_result("сделано")]
    )
    h.tool("pay", writes=True, risk=Risk.HIGH)
    reply = await h.handle("переведи")
    pending_id = reply.pending_id or ""

    first = await h.resume(pending_id, True)
    second = await h.resume(pending_id, True)

    assert first.text == "сделано"
    assert "устарело" in second.text or "обработано" in second.text
    assert h.recorder.calls == [("pay", {"value": "1"})]


async def test_pending_from_other_owner_is_ignored() -> None:
    h = harness([make_chat_result(None, [("c1", "pay", {"value": "1"})])])
    h.tool("pay", writes=True, risk=Risk.HIGH)
    reply = await h.handle("переведи", owner_id=1)
    hijack = await h.resume(reply.pending_id or "", True, owner_id=999)
    assert "другому диалогу" in hijack.text
    assert h.recorder.calls == []


async def test_snapshot_in_kv_is_json_only() -> None:
    """Снимок уходит в Redis через orjson: никакие «живые» объекты туда попасть не должны."""
    h = harness([make_chat_result(None, [("c1", "pay", {"value": "1"})])])
    h.tool("pay", writes=True, risk=Risk.HIGH)
    await h.handle("переведи")

    snapshot = h.pending()
    assert snapshot["actions"][0]["tool"] == "pay"
    assert orjson.loads(orjson.dumps(snapshot)) == snapshot
    assert any(m["role"] == "assistant" for m in snapshot["messages"])


async def test_untrusted_source_write_requires_confirmation() -> None:
    h = harness([make_chat_result(None, [("c1", "note", {"value": "x"})])])
    h.tool("note", writes=True)  # LOW + auto_allow: без untrusted было бы разрешено
    reply = await h.handle("сохрани", source_trust="untrusted")
    assert reply.needs_confirmation
    assert h.recorder.calls == []


# ------------------------------------------------------------------ деградация, память, промпт


async def test_budget_exhausted_degrades_instead_of_crashing() -> None:
    h = harness([BudgetExceeded("дневной бюджет $1.00 исчерпан")])
    reply = await h.handle("расскажи про котиков")
    assert reply.degraded is True
    assert "бюджет" in reply.text.lower()


async def test_model_unavailable_degrades() -> None:
    h = harness([ModelUnavailable("все провайдеры недоступны")])
    reply = await h.handle("что нового?")
    assert reply.degraded is True
    assert "недоступны" in reply.text


async def test_kill_switch_blocks_writes_but_not_reads() -> None:
    h = harness(
        [
            make_chat_result(None, [("c1", "pay", {"value": "1"})]),
            make_chat_result("записи на паузе"),
        ],
        kill_active=True,
    )
    h.tool("pay", writes=True, risk=Risk.LOW)
    reply = await h.handle("запиши")
    assert h.recorder.calls == []
    assert reply.text == "записи на паузе"
    denied = [m for m in h.gateway.calls[1]["messages"] if m.get("role") == "tool"][-1]
    assert "DENIED" in denied["content"]


async def test_history_keeps_dialog_and_drops_tool_noise() -> None:
    h = harness(
        [make_chat_result(None, [("c1", "search", {"value": "x"})]), make_chat_result("итог")]
    )
    h.tool("search")
    await h.handle("ищи", owner_id=5)

    history = h.history(5)
    assert {m["role"] for m in history} <= {"user", "assistant"}
    assert history[-1]["content"] == "итог"
    assert not any("ожидает подтверждения" in m["content"] for m in history)


async def test_reset_clears_context_but_not_database() -> None:
    h = harness([make_chat_result("ок")])
    await h.handle("привет", owner_id=3)
    assert h.history(3)

    await h.supervisor.reset(3)

    assert h.history(3) == []
    assert h.events_of("conversation.reset")


async def test_system_prompt_carries_facts_rules_and_version() -> None:
    h = harness([make_chat_result("привет!")])
    await h.handle("привет")

    system = h.gateway.calls[0]["messages"][0]
    assert system["role"] == "system"
    assert "не пьёт кофе после 16" in system["content"]
    assert "<untrusted>" in system["content"], "правило про untrusted обязано быть в промпте"
    turn = h.events_of("conversation.turn_received")[0]
    assert turn["payload"]["prompt_version"].startswith("sys-v")


async def test_database_outage_does_not_break_answers() -> None:
    class BrokenFacts:
        async def recent(self, limit: int = 50) -> list[str]:
            raise RuntimeError("connection refused")

        async def list(self, limit: int = 50) -> list[Fact]:
            return []

        async def add(self, *args: Any, **kwargs: Any) -> str:
            raise RuntimeError("connection refused")

        async def invalidate(self, fact_id: str) -> bool:
            return False

    h = harness([make_chat_result("жив без БД")], facts=BrokenFacts())
    reply = await h.handle("как дела?")
    assert reply.text == "жив без БД"
    system = h.gateway.calls[0]["messages"][0]
    assert "память недоступна" in system["content"]


async def test_status_reports_mode_without_secrets() -> None:
    h = harness([])
    status = await h.supervisor.status(1)
    text = orjson.dumps(status).decode()
    assert "prompt_version" in status and "cost" in status
    assert "test-key" not in text and "api_key" not in text


@pytest.mark.parametrize("approved", [True, False])
async def test_resume_after_unknown_id_is_polite(approved: bool) -> None:
    h = harness([])
    reply = await h.resume("нет-такого", approved)
    assert "устарело" in reply.text or "обработано" in reply.text
