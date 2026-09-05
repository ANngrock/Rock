"""Экспорт журнала в Langfuse на живом Postgres: SQL-запрос, блобы и детерминированные id.

Офлайн-тесты проверяют арифметику спанов и транспорт. Здесь проверяется то, что без базы не
проверяется вовсе: что окно читается одним запросом с `LEFT JOIN platform.blobs`, что кириллица
доезжает из `bytea` целой, что «срезано при сохранении» различается с «не сохранено», и — главное —
что два прогона по одному и тому же окну дают **байт в байт одно тело**. На этом держится весь
дизайн «без таблицы прогресса»: если бы id зависели от времени или случайности, повторный тик
плодил бы дубли в витрине.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import orjson
import pytest
import pytest_asyncio
from sqlalchemy import text

from aegis.governance.langfuse import LangfuseTarget, collect, export_window, to_otlp
from aegis.governance.recorder import SqlDecisionRecorder
from aegis.platform.config import Settings, override_settings
from aegis.platform.db import reset_engine, session
from aegis.platform.gateway.client import LLMCallRecord

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("AEGIS_TEST_DATABASE_URL"), reason="нужен Postgres"),
]


@pytest_asyncio.fixture
async def db() -> AsyncIterator[None]:
    url = os.environ["AEGIS_TEST_DATABASE_URL"]
    with override_settings(database_url=url):
        reset_engine()
        try:
            async with session() as s:
                await s.execute(text("SELECT 1 FROM governance.decision_records LIMIT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"миграция 0002 не накатана ({type(exc).__name__}) — `make migrate`")
        yield
        reset_engine()


def _cfg(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "_env_prefix": "T_",
        "glm_api_key": "k",
        "langfuse_enabled": True,
        "langfuse_host": "http://langfuse:3000",
        "langfuse_public_key": "pk-lf-test",
        "langfuse_secret_key": "sk-lf-test",
    }
    base.update(over)
    return Settings(**base)  # type: ignore[call-arg]


async def _one_turn(
    recorder: SqlDecisionRecorder, owner_id: int = 777, *, big: bool = False
) -> str:
    """Ход журнала из четырёх записей — ровно тот набор, который должен стать пятью спанами."""
    trace = str(uuid.uuid4())
    prompts = [{"id": "supervisor/system", "version": "0.3", "sha256": "a" * 64}]
    recorder.begin_turn(trace, owner_id=owner_id, prompt_ids=prompts)
    await recorder.on_llm_call(
        LLMCallRecord(
            call_id=str(uuid.uuid4()),
            trace_id=trace,
            role="brain",
            model="glm-4.7",
            prompt_tokens=120,
            completion_tokens=40,
            cost_usd=0.00123,
            latency_ms=777,
            ok=True,
            request={
                "messages": [{"role": "user", "content": "привет, как дела"}],
                "context": "x" * 20_000 if big else "",
            },
            response={"choices": [{"message": {"content": "хорошо, спасибо"}}]},
        )
    )
    await recorder.policy(
        trace_id=trace,
        turn_no=1,
        owner_id=owner_id,
        tool="pay",
        decision="deny",
        reason="платёж без подтверждения",
        risk="high",
    )
    await recorder.tool_run(
        trace_id=trace,
        turn_no=1,
        owner_id=owner_id,
        tool="notes_search",
        args={"query": "кофе"},
        result="одна заметка",
        trust="owner",
        decision="allowed",
        ok=True,
        latency_ms=15,
    )
    await recorder.turn_summary(
        trace_id=trace,
        turn_no=1,
        owner_id=owner_id,
        messages=[{"role": "user", "content": "привет, как дела"}],
        answer="хорошо, спасибо",
        model="glm-4.7",
        cost_usd=0.00123,
        latency_ms=900,
        iterations=1,
    )
    recorder.end_turn(trace)
    return trace


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


def _since() -> datetime:
    return datetime.now(UTC) - timedelta(hours=1)


@pytest.mark.usefixtures("db")
async def test_turn_becomes_root_span_plus_one_per_record() -> None:
    recorder = SqlDecisionRecorder(_cfg())
    trace = await _one_turn(recorder)

    traces = await collect(since=_since(), limit=500, trace_id=trace)
    assert len(traces) == 1, "один ход — один трейс"
    spans = traces[0]
    assert len(spans) == 5, "корень + 4 записи журнала"
    attrs = _attrs(spans[0])
    assert attrs["langfuse.trace.metadata.aegis.trace_id"] == trace
    assert attrs["langfuse.user.id"] == "777"

    kinds = [span["name"] for span in spans[1:]]
    assert kinds == ["llm_call:glm-4.7", "policy", "tool_run", "turn_summary"]
    generation = _attrs(spans[1])
    assert orjson.loads(generation["langfuse.observation.usage_details"]) == {
        "input": 120,
        "output": 40,
        "total": 160,
    }
    assert generation["langfuse.observation.model.name"] == "glm-4.7"
    refs = orjson.loads(generation["langfuse.observation.metadata.aegis.prompt_ids"])
    assert refs == [{"id": "supervisor/system", "version": "0.3", "sha256": "a" * 64}]
    assert _attrs(spans[2])["langfuse.observation.level"] == "ERROR", (
        "deny — это уровень, а не цвет"
    )


@pytest.mark.usefixtures("db")
async def test_cyrillic_and_json_survive_the_blob_round_trip() -> None:
    recorder = SqlDecisionRecorder(_cfg())
    trace = await _one_turn(recorder)

    spans = (await collect(since=_since(), limit=500, trace_id=trace))[0]
    exported = _attrs(spans[1])
    # сравниваем разобранный JSON, а не подстроку: блобы хранятся каноническим ASCII-JSON,
    # и кириллица в них экранирована — это ожидаемо
    assert orjson.loads(exported["langfuse.observation.input"])["messages"] == [
        {"role": "user", "content": "привет, как дела"}
    ]
    parsed = orjson.loads(exported["langfuse.observation.output"])
    assert parsed["choices"][0]["message"]["content"] == "хорошо, спасибо"


@pytest.mark.usefixtures("db")
async def test_exporter_reads_the_same_content_the_replayer_reads() -> None:
    """Содержимое витрины = содержимое воспроизведения; иначе это второй журнал, а не витрина."""
    recorder = SqlDecisionRecorder(_cfg())
    trace = await _one_turn(recorder)
    records = await recorder.records_for_trace(trace)
    spans = (await collect(since=_since(), limit=500, trace_id=trace))[0]
    for index, record in enumerate(records):
        exported = _attrs(spans[index + 1])
        want = record.get("input") or ""
        if want:
            assert exported.get("langfuse.observation.input") == want[:6_000]


@pytest.mark.usefixtures("db")
async def test_two_runs_of_the_same_window_send_the_same_bytes() -> None:
    recorder = SqlDecisionRecorder(_cfg())
    trace = await _one_turn(recorder)

    first = orjson.dumps(to_otlp(await collect(since=_since(), limit=500, trace_id=trace)))
    second = orjson.dumps(to_otlp(await collect(since=_since(), limit=500, trace_id=trace)))
    assert first == second, "идемпотентность — не обещание кода, а свойство данных"
    payload = orjson.loads(first)
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["traceId"] == trace.replace("-", ""), "UUID хода и есть OTLP traceId"


@pytest.mark.usefixtures("db")
async def test_window_filter_excludes_the_future_and_other_traces() -> None:
    recorder = SqlDecisionRecorder(_cfg())
    trace = await _one_turn(recorder)

    later = await collect(since=datetime.now(UTC) + timedelta(hours=1), limit=500, trace_id=trace)
    assert later == [], "окно должно уважать `since`, иначе каждый тик читает всю историю"
    other = await collect(since=_since(), limit=500, trace_id=str(uuid.uuid4()))
    assert other == [], "фильтр по ходу работает: чужие строки не приплетаются"


@pytest.mark.usefixtures("db")
async def test_truncated_content_is_marked_as_truncated() -> None:
    """Урезанное при сохранение — не «весь ответ»: в витрине это должно быть подписано."""
    recorder = SqlDecisionRecorder(_cfg(repro_max_blob_bytes=4_096))
    trace = await _one_turn(recorder, big=True)

    spans = (await collect(since=_since(), limit=500, trace_id=trace))[0]
    flags = [
        _attrs(span).get("langfuse.observation.metadata.aegis.truncated") for span in spans[1:]
    ]
    assert any(flags), "при tiny-лимите усечение обязано быть помечено"


@pytest.mark.usefixtures("db")
async def test_export_against_a_fake_server_posts_our_spans() -> None:
    recorder = SqlDecisionRecorder(_cfg())
    trace = await _one_turn(recorder)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(207, text='{"successes":[],"errors":[]}')

    target = LangfuseTarget.from_settings(_cfg())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await export_window(
            target=target,
            cfg=_cfg(langfuse_limit=500),
            since=_since(),
            trace_id=trace,
            client=client,
        )
    assert report.ok and report.traces == 1 and report.spans == 5 and report.batches == 1
    assert len(seen) == 1
    assert seen[0].url.path == "/api/public/otel/v1/traces"
    assert seen[0].headers["x-langfuse-ingestion-version"] == "4"
    body = orjson.loads(seen[0].content)
    assert body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] == trace.replace(
        "-", ""
    )
    assert body["resourceSpans"][0]["resource"]["attributes"][0]["key"] == "service.name"
