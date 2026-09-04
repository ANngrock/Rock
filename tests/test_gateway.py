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
    # имя берётся из роли (у этого конфига FALLBACK_MODEL не задан) — сверяем с каталогом,
    # чтобы тест не протухал при смене дефолтной модели
    assert meta["fallback"].requests[0]["model"] == CATALOG["brain"].name
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
    # модель с ненулевым прайсом: на бесплатном дефолте оценка стоит $0, и гейт проверять нечего
    cfg = Settings(_env_file=None, _env_prefix="T_", glm_api_key="k", model_brain="glm-4.6")
    gateway, _kv, primary, _records, _meta = make_gateway(
        [response("не должно случиться")], cfg=cfg, budget=1.0
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


async def test_thinking_param_can_be_switched_off_for_routers() -> None:
    """Роутер, не знающий нестандартного параметра thinking, не должен получать 400 из-за него."""
    cfg = Settings(
        _env_file=None,
        _env_prefix="T_",
        glm_api_key="k",
        llm_backoff_s=0.05,
        llm_thinking_param=False,
    )
    gateway, _kv, primary, _records, _meta = make_gateway([response("ок")], cfg=cfg)
    await gateway.chat("brain", [{"role": "user", "content": "привет"}], thinking=True)
    assert "extra_body" not in primary.requests[0]


async def test_thinking_always_on_model_never_asks_to_disable_it() -> None:
    """Деградация бюджета не должна превращаться в 400 «thinking.type disabled unsupported»."""
    cfg = Settings(
        _env_file=None,
        _env_prefix="T_",
        glm_api_key="k",
        llm_backoff_s=0.05,
        model_brain="z-ai/glm-5.3-flash",
    )
    gateway, _kv, primary, _records, _meta = make_gateway([response("ок")], cfg=cfg)
    await gateway.chat("brain", [{"role": "user", "content": "привет"}], thinking=False)
    assert primary.requests[0]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert primary.requests[0]["model"] == "z-ai/glm-5.3-flash"


async def test_embeddings_use_their_own_client() -> None:
    """Роутер может не проксировать эмбеддинги: для них отдельный эндпоинт и ключ."""
    cfg = Settings(
        _env_file=None,
        _env_prefix="T_",
        glm_api_key="k",
        llm_backoff_s=0.05,
        embed_base_url="https://api.z.ai/api/paas/v4/",
    )
    gateway, _kv, _primary, _records, _meta = make_gateway([], cfg=cfg)
    used: list[str] = []

    async def embed_create(**kwargs: Any) -> Any:  # noqa: ANN401
        used.append("embed")

        class Item:
            embedding = [0.0] * 4

        class Resp:
            data = [Item()]
            usage = SimpleNamespace(total_tokens=3)

        return Resp()

    async def primary_create(**kwargs: Any) -> Any:  # noqa: ANN401
        used.append("primary")
        raise AssertionError("primary не должен использоваться для эмбеддингов")

    gateway.embed_client = SimpleNamespace(  # type: ignore[assignment]
        embeddings=SimpleNamespace(create=embed_create)
    )
    gateway.primary = SimpleNamespace(  # type: ignore[assignment]
        embeddings=SimpleNamespace(create=primary_create), close=_noop
    )
    assert await gateway.embed(["текст"]) == [[0.0] * 4]
    assert used == ["embed"]


async def test_embeddings_fall_back_to_primary_endpoint() -> None:
    """Без EMBED_BASE_URL векторы просим у основного провайдера — ничего не ломаем."""
    gateway, _kv, _primary, _records, _meta = make_gateway([])

    async def create(**kwargs: Any) -> Any:  # noqa: ANN401
        class Item:
            embedding = [0.1] * 2

        class Resp:
            data = [Item()]
            usage = SimpleNamespace(total_tokens=1)

        return Resp()

    gateway.primary = SimpleNamespace(embeddings=SimpleNamespace(create=create), close=_noop)  # type: ignore[assignment]
    gateway.embed_client = gateway.primary
    assert await gateway.embed(["текст"]) == [[0.1] * 2]


# ------------------------------------------------- ключ vs endpoint (причина 401 без логов)


async def test_auth_hint_reports_foreign_key() -> None:
    from aegis.platform.config import Settings
    from aegis.platform.gateway.client import ModelGateway

    cfg = Settings(
        _env_file=None,
        glm_api_key="sk-ai-v1-abcdef0123",
        glm_base_url="https://zenmux.ai/api/v1/",
        kv_backend="memory",
    )
    gateway = ModelGateway(cfg, None)  # type: ignore[arg-type]
    hint = gateway.auth_hint()
    assert "z.ai" in hint and "zenmux.ai" in hint
    describe = gateway.describe()
    assert describe["endpoint"] == "zenmux.ai"
    assert describe["auth_hint"] == hint
    assert "abcdef0123" not in hint, "наружу уходит только префикс"
    await gateway.aclose()


async def test_auth_hint_quiet_when_key_matches_endpoint() -> None:
    from aegis.platform.config import Settings
    from aegis.platform.gateway.client import ModelGateway

    cfg = Settings(
        _env_file=None,
        glm_api_key="sk-ss-v1-abcdef0123",
        glm_base_url="https://zenmux.ai/api/v1/",
        kv_backend="memory",
    )
    gateway = ModelGateway(cfg, None)  # type: ignore[arg-type]
    assert gateway.auth_hint() == ""
    await gateway.aclose()


# --------------------------------------------------------------- резерв: цена по имени модели


async def test_fallback_enabled_by_model_name_alone() -> None:
    """FALLBACK_MODEL без ключа/адреса = резерв на том же провайдере: дублировать секрет незачем."""
    cfg = Settings(
        _env_file=None,
        _env_prefix="T_",
        glm_api_key="sk-ai-v1-x",
        glm_base_url="https://api.z.ai/api/paas/v4/",
        fallback_model="glm-4.7-flashx",
        llm_backoff_s=0.05,
    )
    gateway, _kv, _primary, _records, _meta = make_gateway([response("ок")], cfg=cfg)
    assert gateway.fallback is not None
    describe = gateway.describe()
    assert describe["fallback_enabled"] is True
    assert describe["fallback_model"] == "glm-4.7-flashx"


async def test_fallback_call_is_costed_by_fallback_model() -> None:
    """429 на бесплатной основной модели -> платный резерв, и его стоимость учтена, а не 0.

    Учёт «по цене роли» на бесплатной основной модели давал бы $0.000000 на каждом резервном
    запросе: дневной бюджет выглядел бы соблюдённым, пока провайдер режет частоту.
    """
    cfg = Settings(
        _env_file=None,
        _env_prefix="T_",
        glm_api_key="k",
        model_brain="glm-4.7-flash",  # free-тиер: 0/0
        fallback_model="glm-4.7-flashx",  # 0.07/0.4
        llm_backoff_s=0.05,
    )
    gateway, _kv, _primary, records, meta = make_gateway(
        [api_error(429)],
        fallback_script=[response("ок", prompt_tokens=1000, completion_tokens=500)],
        cfg=cfg,
    )
    result = await gateway.chat("brain", [{"role": "user", "content": "сводка"}])
    expected = (1000 * 0.07 + 500 * 0.4) / 1_000_000
    assert result.cost_usd == pytest.approx(expected, abs=1e-12)
    assert await gateway.cost.spent() == pytest.approx(expected, abs=1e-12)
    assert meta["fallback"].requests[0]["model"] == "glm-4.7-flashx"
    ok = [r for r in records if r.ok]
    assert len(ok) == 1 and ok[0].provider == "fallback" and ok[0].cost_usd > 0


# --------------------------------------------------------------- форма thinking-параметра


async def test_thinking_body_shape_follows_the_provider() -> None:
    """z.ai ждёт `thinking.type`, OpenRouter — `reasoning.enabled`: неверная форма = 400."""
    from aegis.platform.config import Settings as _S

    or_cfg = _S(
        _env_file=None,
        _env_prefix="T_",
        glm_api_key="k",
        glm_base_url="https://openrouter.ai/api/v1/",
        model_brain="z-ai/glm-5.2",
    )
    gateway, *_rest = make_gateway([response("ок")], cfg=or_cfg)
    assert gateway._thinking_body(True) == {"reasoning": {"enabled": True}}
    assert gateway._thinking_body(False) == {"reasoning": {"enabled": False}}

    zai_cfg = _S(
        _env_file=None,
        _env_prefix="T_",
        glm_api_key="k",
        glm_base_url="https://api.z.ai/api/paas/v4/",
        model_brain="glm-5.3-flash",
    )
    gateway2, _kv2, primary2, _rec2, _meta2 = make_gateway([response("ок")], cfg=zai_cfg)
    await gateway2.chat("brain", [{"role": "user", "content": "го"}], thinking=False)
    # 5.3-серия не выключается: вместо «disabled» шлём «enabled», иначе провайдер отвечает 400
    assert primary2.requests[0]["extra_body"] == {"thinking": {"type": "enabled"}}

    forced = _S(
        _env_file=None,
        _env_prefix="T_",
        glm_api_key="k",
        glm_base_url="https://openrouter.ai/api/v1/",
        llm_thinking_style="zai",
    )
    gateway3, *_rest3 = make_gateway([response("ок")], cfg=forced)
    assert gateway3._thinking_body(True) == {"thinking": {"type": "enabled"}}


async def test_openrouter_never_disables_always_on_models() -> None:
    """На роутере тоже: thinking-always-on модель не должна получить reasoning.enabled=false."""
    cfg = Settings(
        _env_file=None,
        _env_prefix="T_",
        glm_api_key="k",
        glm_base_url="https://openrouter.ai/api/v1/",
        model_brain="z-ai/glm-5.3-flash",
    )
    gateway, _kv, primary, _records, _meta = make_gateway([response("ок")], cfg=cfg)
    await gateway.chat("brain", [{"role": "user", "content": "го"}], thinking=False)
    assert primary.requests[0]["extra_body"] == {"reasoning": {"enabled": True}}


async def test_openrouter_request_carries_reasoning_not_thinking() -> None:
    cfg = Settings(
        _env_file=None,
        _env_prefix="T_",
        glm_api_key="k",
        glm_base_url="https://openrouter.ai/api/v1/",
        model_brain="z-ai/glm-5.2",
    )
    gateway, _kv, primary, _records, _meta = make_gateway([response("ок")], cfg=cfg)
    await gateway.chat("brain", [{"role": "user", "content": "план"}], thinking=True)
    assert primary.requests[0]["extra_body"] == {"reasoning": {"enabled": True}}
