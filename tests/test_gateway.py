"""ModelGateway: retry/fallback, бюджет, DLP, деградация, аудит каждого attempt.

Здесь подменён только сетевой клиент — вся остальная логика (включая openai-исключения) реальная.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from openai import APIStatusError

from aegis.platform.config import Settings
from aegis.platform.gateway.client import LLMCallRecord, ModelGateway, ModelUnavailable
from aegis.platform.gateway.cost import BudgetExceeded, CostGovernor
from aegis.platform.gateway.models import CATALOG
from conftest import FakeKV


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
        model_dump=lambda exclude_none: {"role": "assistant", "content": content},
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


def make_gateway(
    primary_script: list[Any],
    *,
    fallback_script: list[Any] | None = None,
    cfg: Settings | None = None,
    budget: float = 10.0,
) -> tuple[ModelGateway, FakeKV, FakeCompletions, list[LLMCallRecord], dict[str, Any]]:
    cfg = cfg or Settings(_env_file=None, _env_prefix="T_", glm_api_key="k", llm_backoff_s=0.05)
    kv = FakeKV()
    cost = CostGovernor(kv, daily_limit_usd=budget)  # type: ignore[arg-type]
    gateway = ModelGateway(cfg, cost)
    primary = FakeCompletions(primary_script)
    gateway.primary = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=primary), embeddings=None, close=_noop
    )
    fallback_client = None
    if fallback_script is not None:
        fallback = FakeCompletions(fallback_script)
        gateway.fallback = SimpleNamespace(chat=SimpleNamespace(completions=fallback), close=_noop)  # type: ignore[assignment]
        fallback_client = fallback
    records: list[LLMCallRecord] = []

    async def record(item: LLMCallRecord) -> None:
        records.append(item)

    gateway.recorder = record
    meta = {"primary": primary, "fallback": fallback_client}
    return gateway, kv, primary, records, meta


async def _noop() -> None:
    return None


# ------------------------------------------------------------------ успех


async def test_successful_call_costs_and_records() -> None:
    gateway, _kv, primary, records, _meta = make_gateway([response("ок")])
    result = await gateway.chat("brain", [{"role": "user", "content": "привет"}])

    assert result.content == "ок"
    assert result.model == "glm-test"
    assert result.latency_ms >= 0
    expected = (
        1000 * CATALOG["brain"].in_usd_per_m + 500 * CATALOG["brain"].out_usd_per_m
    ) / 1_000_000
    assert result.cost_usd == pytest.approx(expected, abs=1e-9)
    assert await gateway.cost.spent() == pytest.approx(expected, abs=1e-9)
    assert records and records[0].ok is True and records[0].prompt_tokens == 1000


async def test_tools_and_thinking_are_passed_to_provider() -> None:
    gateway, _kv, primary, _records, _meta = make_gateway([response("ок")])
    await gateway.chat(
        "brain",
        [{"role": "user", "content": "подумай"}],
        tools=[
            {"type": "function", "function": {"name": "x", "description": "d", "parameters": {}}}
        ],
        thinking=True,
    )
    sent = primary.requests[0]
    assert sent["tool_choice"] == "auto"
    assert sent["extra_body"] == {"thinking": {"type": "enabled"}}


async def test_thinking_is_disabled_when_spec_does_not_support_it() -> None:
    gateway, _kv, primary, _records, _meta = make_gateway([response("ок")])
    await gateway.chat("fast", [{"role": "user", "content": "привет"}], thinking=True)
    assert "extra_body" not in primary.requests[0]


# ------------------------------------------------------------------ DLP


async def test_pii_never_reaches_the_provider_and_is_restored_in_reply() -> None:
    gateway, _kv, primary, _records, _meta = make_gateway(
        [response("записал карту <AEGIS_PII:CARD:1>")]
    )
    result = await gateway.chat(
        "brain", [{"role": "user", "content": "сохрани карту 2202 2036 5151 2225"}]
    )

    sent_text = primary.requests[0]["messages"][0]["content"]
    assert "2202" not in sent_text and "AEGIS_PII:CARD:1" in sent_text
    assert result.content == "записал карту 2202 2036 5151 2225"


# ------------------------------------------------------------------ retry / fallback


async def test_rate_limit_is_retried_then_succeeds() -> None:
    gateway, _kv, primary, records, _meta = make_gateway(
        [api_error(429), api_error(429), response("после ретраев")]
    )
    result = await gateway.chat("brain", [{"role": "user", "content": "go"}])

    assert result.content == "после ретраев"
    assert len(primary.requests) == 3
    assert [r.ok for r in records] == [False, False, True]


async def test_permanent_error_switches_provider_immediately() -> None:
    gateway, _, primary, records, meta = make_gateway(
        [api_error(400)], fallback_script=[response("через fallback")]
    )
    result = await gateway.chat("brain", [{"role": "user", "content": "go"}])

    assert result.content == "через fallback"
    assert len(primary.requests) == 1, "400 повторять бессмысленно"
    assert meta["fallback"].requests[0]["model"] == "glm-4.6"
    assert [r.provider for r in records] == ["primary", "fallback"]


async def test_5xx_retries_exhausted_then_fallback() -> None:
    gateway, _kv, primary, _records, meta = make_gateway(
        [api_error(503), api_error(503), api_error(503)], fallback_script=[response("живы")]
    )
    result = await gateway.chat("brain", [{"role": "user", "content": "go"}])
    assert result.content == "живы"
    assert len(primary.requests) == 3  # retries_per_client=2 → 3 попытки
    assert meta["fallback"] is not None


async def test_all_providers_down_raises_typed_error() -> None:
    gateway, _kv, _primary, records, _meta = make_gateway(
        [api_error(500), api_error(500), api_error(500)]
    )
    with pytest.raises(ModelUnavailable, match="все провайдеры"):
        await gateway.chat("brain", [{"role": "user", "content": "go"}])
    assert all(r.ok is False for r in records)
    assert all(r.error for r in records)


# ------------------------------------------------------------------ бюджет и деградация


async def test_budget_gate_blocks_before_request() -> None:
    gateway, _kv, primary, _records, _meta = make_gateway(
        [response("не должно случиться")], budget=1.0
    )
    await gateway.cost.record(0.999)
    with pytest.raises(BudgetExceeded):
        await gateway.chat("brain", [{"role": "user", "content": "дорого"}])
    assert primary.requests == []
    assert _records == []


async def test_degradation_turns_thinking_off_then_switches_model() -> None:
    cfg = Settings(_env_file=None, _env_prefix="T_", glm_api_key="k", llm_backoff_s=0.05)
    gateway, _kv, primary, _records, _meta = make_gateway(
        [response("а"), response("б")], cfg=cfg, budget=1.0
    )

    await gateway.cost.record(0.7)
    await gateway.chat("brain", [{"role": "user", "content": "x"}], thinking=True)
    assert primary.requests[0]["extra_body"]["thinking"]["type"] == "disabled"

    await gateway.cost.record(0.25)  # 0.95 → уровень 2
    await gateway.chat("brain", [{"role": "user", "content": "y"}])
    assert primary.requests[1]["model"] == CATALOG["fast"].name


# ------------------------------------------------------------------ embed


async def test_embed_records_usage_and_returns_vectors() -> None:
    gateway, _kv, _primary, _records, _meta = make_gateway([response("ок")])

    async def create(model: str, input: list[str]) -> Any:
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.1, 0.2]) for _ in input],
            usage=SimpleNamespace(total_tokens=42, prompt_tokens=42, completion_tokens=0),
        )

    gateway.primary = SimpleNamespace(  # type: ignore[assignment]
        chat=None, embeddings=SimpleNamespace(create=create), close=_noop
    )
    vectors = await gateway.embed(["а", "б"])
    assert vectors == [[0.1, 0.2], [0.1, 0.2]]
    assert _records[-1].role == "embed" and _records[-1].prompt_tokens == 42
    assert await gateway.cost.spent() > 0


async def test_embed_failure_raises_model_unavailable() -> None:
    gateway, _kv, _primary, records, _meta = make_gateway([response("ок")])

    async def create(model: str, input: list[str]) -> Any:
        raise APIStatusError(
            "boom",
            response=__import__("httpx").Response(
                500, request=__import__("httpx").Request("POST", "https://x")
            ),
            body=None,
        )

    gateway.primary = SimpleNamespace(
        chat=None, embeddings=SimpleNamespace(create=create), close=_noop
    )  # type: ignore[assignment]
    with pytest.raises(ModelUnavailable):
        await gateway.embed(["а"])
    assert records[-1].ok is False


def test_describe_does_not_leak_keys() -> None:
    import json

    cfg = Settings(
        _env_file=None, _env_prefix="T_", glm_api_key="sk-super-secret", fallback_api_key="sk-fb"
    )
    gateway, _kv, _primary, _records, _meta = make_gateway(
        [], fallback_script=[response("x")], cfg=cfg
    )
    dumped = json.dumps(gateway.describe(), ensure_ascii=False)
    assert "sk-super-secret" not in dumped
    assert "sk-fb" not in dumped
    assert "brain" in dumped
