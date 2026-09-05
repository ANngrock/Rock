"""Общие фикстуры тестов.

Тесты шага 1 не требуют ни Postgres, ни Redis, ни сети: fake-объекты подменяют внешние миры,
а интеграционные проверки (event store, pgvector) помечены ``@pytest.mark.integration`` и
включаются только при заданном ``AEGIS_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import orjson
import pytest
from openai import APIStatusError  # общий двойник транспорта

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
from aegis.platform.gateway.client import (  # noqa: E402
    ChatResult,
    LLMCallRecord,
    ModelGateway,
    ToolCall,
)
from aegis.platform.gateway.cost import CostGovernor  # noqa: E402
from aegis.platform.gateway.dlp import DLP  # noqa: E402


class FakeGateway:
    """Заглушка ModelGateway: скрипт ответов + тот же CostGovernor/DLP, что в проде."""

    def __init__(
        self,
        responses: list[ChatResult | Exception],
        cost: CostGovernor,
        *,
        judgements: list[Any] | None = None,
    ) -> None:
        self._responses = list(responses)
        self.judgements: list[Any] = list(judgements or [])
        self.cost = cost
        self.dlp = DLP()
        self.calls: list[dict[str, Any]] = []
        self.json_calls: list[dict[str, Any]] = []
        #: что ушло в стриминговый путь: отдельный список, чтобы `calls` не менял форму
        self.stream_calls: list[dict[str, Any]] = []
        self.embed_calls = 0
        self.auth_hint_text = ""

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

    async def chat_stream(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        thinking: bool = False,
        trace_id: str | None = None,
        on_text: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Стриминговый путь: тот же скрипт, но текст отдаётся кусками — как у провайдера.

        Куски по 7 знаков, а не «весь ответ одним delta»: одним куском не проверить ни склейку, ни
        то, что интерфейс видел ровно те же символы, что попали в ответ. `calls` остаётся той же
        формы, что и у `chat` — тесты сверяют содержательные поля, а не различие путей.
        """
        self.calls.append(
            {"role": role, "tools": tools, "thinking": thinking, "messages": messages}
        )
        self.stream_calls.append({"role": role, "trace_id": trace_id, "deltas": []})
        if not self._responses:
            raise AssertionError("FakeGateway: скрипт ответов исчерпан")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        content = item.content if isinstance(item.content, str) else ""
        for start in range(0, len(content), 7):
            piece = content[start : start + 7]
            self.stream_calls[-1]["deltas"].append(piece)
            if on_text is not None:
                await on_text(piece)
        return item

    async def chat_json(
        self, role: str, messages: list[dict[str, Any]], schema: Any, **kwargs: Any
    ) -> Any:
        """Structured-вызовы (судья сверки, карантинный разборщик) — со своим скриптом.

        Отдельный скрипт, а не общий с ``chat``: иначе тест сверки съедал бы ответ, назначенный
        мозгу, и падал на пустом скрипте — ровно тот класс ложных падений, из-за которого двойников
        начинают обходить настоящими сетевыми вызовами.
        """
        self.json_calls.append(
            {"role": role, "messages": messages, "schema": schema.__name__, **kwargs}
        )
        if not self.judgements:
            raise AssertionError("FakeGateway: нет скрипта structured-ответов")
        item = self.judgements.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item if isinstance(item, schema) else schema.model_validate(item)

    async def embed(self, texts: list[str], *, trace_id: str | None = None) -> list[list[float]]:
        self.embed_calls += 1
        return [[0.1, 0.2, 0.3] for _ in texts]

    def auth_hint(self) -> str:
        return self.auth_hint_text

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
    def __init__(self, hits: list[NoteHit] | None = None, *, pending: int = 0) -> None:
        self.hits = hits or []
        self.added: list[Note] = []
        self.pending = pending

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

    async def count_pending(self) -> int:
        return self.pending


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


# ------------------------------------------------------------------ шлюз

#: Двойники для тестов ModelGateway: сеть подменяется целиком, остальная логика (retry, budget,
#: DLP, учёт стоимости, журнал вызовов) — настоящая. Живут здесь, потому что нужны не только
#: test_gateway: снимки полезной нагрузки проверяют и тесты воспроизводимости.


class FakeCompletions:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        item = self.script.pop(0) if self.script else AssertionError("скрипт исчерпан")
        if isinstance(item, BaseException):
            raise item
        return item


def response(
    content: str | None = "ответ", *, prompt_tokens: int = 1000, completion_tokens: int = 500
) -> Any:
    message = SimpleNamespace(
        content=content,
        tool_calls=None,
        model_dump=lambda **kwargs: {"role": "assistant", "content": content},
    )
    return SimpleNamespace(
        model="glm-test",
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, total_tokens=0
        ),
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
    )


def api_error(status: int) -> APIStatusError:
    import httpx

    request = httpx.Request("POST", "https://api.example/v1/chat/completions")
    return APIStatusError(
        f"status {status}", response=httpx.Response(status, request=request), body=None
    )


async def noop() -> None:
    return None


def make_gateway(
    primary_script: list[Any],
    *,
    fallback_script: list[Any] | None = None,
    cfg: Settings | None = None,
    budget: float = 10.0,
) -> tuple[ModelGateway, FakeKV, FakeCompletions, list[LLMCallRecord], dict[str, Any]]:
    """Шлюз с подменённым клиентом и собранными записями вызовов.

    Возвращаем и ``records`` (то, что ушло в аудит/журнал), и ``meta`` с резервным клиентом: тесты
    fallback проверяют, какой именно клиент получил запрос, а не только то, что ответ пришёл.
    """
    cfg = cfg or Settings(_env_file=None, _env_prefix="T_", glm_api_key="k", llm_backoff_s=0.05)
    kv = FakeKV()
    cost = CostGovernor(kv, daily_limit_usd=budget)  # type: ignore[arg-type]
    gateway = ModelGateway(cfg, cost)
    primary = FakeCompletions(primary_script)
    gateway.primary = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=primary), embeddings=None, close=noop
    )
    fallback_client = None
    if fallback_script is not None:
        fallback = FakeCompletions(fallback_script)
        gateway.fallback = SimpleNamespace(chat=SimpleNamespace(completions=fallback), close=noop)  # type: ignore[assignment]
        fallback_client = fallback
    records: list[LLMCallRecord] = []

    async def record(item: LLMCallRecord) -> None:
        records.append(item)

    gateway.recorder = record
    meta = {"primary": primary, "fallback": fallback_client}
    return gateway, kv, primary, records, meta


__all__ = [
    "FakeCompletions",
    "FakeFacts",
    "FakeGateway",
    "FakeKV",
    "FakeNotes",
    "api_error",
    "make_chat_result",
    "make_gateway",
    "noop",
    "response",
]
