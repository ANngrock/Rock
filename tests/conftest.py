"""Общие фикстуры тестов.

Тесты шага 1 не требуют ни Postgres, ни Redis, ни сети: fake-объекты подменяют внешние миры,
а интеграционные проверки (event store, pgvector) помечены ``@pytest.mark.integration`` и
включаются только при заданном ``AEGIS_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import orjson
import pytest

os.environ.setdefault("ENV", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://aegis:aegis@localhost:5432/aegis")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("TIMEZONE", "Europe/Moscow")
os.environ.setdefault("TELEGRAM_OWNER_ID", "42")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("GLM_API_KEY", "test-key")
os.environ.setdefault("LOG_JSON", "false")

from aegis.agents.services import Services  # noqa: E402
from aegis.agents.tools.registry import ToolRegistry  # noqa: E402
from aegis.governance.policy import PolicyEngine  # noqa: E402
from aegis.knowledge.notes import Note, NoteHit  # noqa: E402
from aegis.memory.facts import Fact  # noqa: E402
from aegis.platform.config import Settings, override_settings  # noqa: E402
from aegis.platform.gateway.client import ChatResult, ToolCall  # noqa: E402
from aegis.platform.gateway.cost import CostGovernor  # noqa: E402
from aegis.platform.gateway.dlp import DLP  # noqa: E402


class FakeGateway:
    """Заглушка ModelGateway: скрипт ответов + тот же CostGovernor/DLP, что в проде."""

    def __init__(self, responses: list[ChatResult | Exception], cost: CostGovernor) -> None:
        self._responses = list(responses)
        self.cost = cost
        self.dlp = DLP()
        self.calls: list[dict[str, Any]] = []
        self.embed_calls = 0

    async def chat(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        thinking: bool = False,
        trace_id: str | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(
            {"role": role, "tools": tools, "thinking": thinking, "messages": messages}
        )
        if not self._responses:
            raise AssertionError("FakeGateway: скрипт ответов исчерпан")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def embed(self, texts: list[str], *, trace_id: str | None = None) -> list[list[float]]:
        self.embed_calls += 1
        return [[0.1, 0.2, 0.3] for _ in texts]

    def describe(self) -> dict[str, Any]:
        return {
            "models": {"brain": "fake-brain"},
            "fallback_enabled": False,
            "fallback_model": None,
        }

    async def aclose(self) -> None:
        return None


def make_chat_result(
    content: str | None = "ок",
    tool_calls: list[tuple[str, str, dict[str, Any]]] | None = None,
    *,
    model: str = "fake-brain",
    cost: float = 0.001,
) -> ChatResult:
    calls = [
        ToolCall(id=call_id, name=name, arguments=orjson.dumps(args).decode())
        for call_id, name, args in (tool_calls or [])
    ]
    raw: dict[str, Any] = {"role": "assistant"}
    if content is not None:
        raw["content"] = content
    if calls:
        raw["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in calls
        ]
    return ChatResult(
        content=content,
        tool_calls=calls,
        model=model,
        role="brain",
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=cost,
        latency_ms=12,
        raw_message=raw,
    )


class FakeFacts:
    def __init__(self, facts: list[str] | None = None) -> None:
        self.store: list[Fact] = [
            Fact(id=f"uuid-{i}", fact=f, category="general", importance=0.5)
            for i, f in enumerate(facts or [])
        ]
        self.added: list[tuple[str, str]] = []

    async def add(
        self, fact: str, category: str = "general", *, source: str, importance: float = 0.5
    ) -> str:
        self.added.append((fact, category))
        new_id = f"uuid-{len(self.store) + 1}"
        self.store.append(Fact(id=new_id, fact=fact, category=category, importance=importance))
        return new_id

    async def recent(self, limit: int = 50) -> list[str]:
        return [f.fact for f in self.store[:limit]]

    async def list(self, limit: int = 50) -> list[Fact]:
        return self.store[:limit]

    async def invalidate(self, fact_id: str) -> bool:
        before = len(self.store)
        self.store = [f for f in self.store if f.id != fact_id]
        return len(self.store) < before


class FakeNotes:
    def __init__(self, hits: list[NoteHit] | None = None) -> None:
        self.hits = hits or []
        self.added: list[Note] = []

    async def add(
        self, title: str, body: str = "", tags: list[str] | None = None, *, source: str = "owner"
    ) -> Note:
        note = Note(id=f"note-{len(self.added) + 1}", title=title, body=body, tags=tags or [])
        self.added.append(note)
        return note

    async def search(
        self, query: str, embedding: list[float] | None = None, limit: int = 5
    ) -> list[NoteHit]:
        return self.hits[:limit]


class FakeKV:
    """Двойник Redis на dict: get/set/delete/getdel + incrbyfloat/expire для учёта бюджета."""

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
        self.data[f"ttl:{key}"] = str(seconds).encode()
        return True


@pytest.fixture
def kv() -> Any:
    """Настоящий Redis-протокол, только in-process."""
    fakeredis = pytest.importorskip("fakeredis")
    return fakeredis.aioredis.FakeRedis(decode_responses=False)


@pytest.fixture
def cost(kv: Any) -> CostGovernor:
    return CostGovernor(kv, daily_limit_usd=2.0, timezone="Europe/Moscow")


@pytest.fixture
def settings_override() -> Iterator[Settings]:
    with override_settings(
        telegram_bot_token=None,
        glm_api_key="test-key",
        max_iterations=3,
        pending_ttl_seconds=60,
        history_limit=8,
    ) as cfg:
        yield cfg


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


@pytest.fixture
def services(cost: CostGovernor) -> Services:
    return Services(gateway=FakeGateway([], cost), facts=FakeFacts(), notes=FakeNotes())  # type: ignore[arg-type]


@pytest.fixture
def policy() -> PolicyEngine:
    return PolicyEngine(auto_allow_low_risk=True)


__all__ = ["FakeFacts", "FakeGateway", "FakeKV", "FakeNotes", "make_chat_result"]
