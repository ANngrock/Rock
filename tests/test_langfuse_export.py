"""Экспортёр журнала в Langfuse: витрина над журналом, а не второй журнал.

Что здесь проверяется — три обещания, без которых такой экспорт делать незачем:

1. *повтор безопасен*: идентификаторы выводятся из строк журнала, поэтому «отправить окно ещё раз»
   означает «перезаписать», и нам не нужно состояние прогресса (ни таблицы, ни файла);
2. *отказ витрины не становится отказом приложения*: транспортная ошибка превращается в отчёт с
   причиной и ненулевой код возврата, а не в исключение из пользовательского пути;
3. *данные уходят те же, что и при воспроизведении*: содержимое из тех же blobs, отметка об
   урезании — тоже, и обрезка под витрину не маскируется под полное содержимое.

Сеть не нужна: `httpx.MockTransport` отвечает вместо Langfuse, и этого довольно, чтобы проверить
путь, тело запроса, коды и разбиение на пачки.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import orjson
import pytest

from aegis.governance.langfuse import (
    MAX_REQUEST_BYTES,
    OTLP_TRACES_PATH,
    ExportFailed,
    ExportUnavailable,
    LangfuseTarget,
    _batches,
    collect,
    export_window,
    send,
    spans_for_trace,
    to_otlp,
)
from aegis.platform.config import Settings

TRACE = "11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "_env_prefix": "L_",
        "glm_api_key": "k",
        "langfuse_enabled": True,
        "langfuse_host": "https://cloud.langfuse.com",
        "langfuse_public_key": "pk-lf-1234567890",
        "langfuse_secret_key": "sk-lf-abcdefghij",
    }
    base.update(over)
    return Settings(**base)


def _record(**over: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": "rec-1",
        "trace_id": TRACE,
        "turn_no": 3,
        "seq": 10,
        "kind": "turn_summary",
        "owner_id": 7,
        "prompt_ids": [],
        "model": None,
        "params": {},
        "policy": None,
        "cost_usd": 0,
        "latency_ms": 0,
        "truncated": False,
        "note": None,
        "hash": bytes(range(32)),
        "created_at": NOW,
        "input_text": "",
        "output_text": "",
    }
    record.update(over)
    return record


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in span["attributes"]:
        value = item["value"]
        out[item["key"]] = next(iter(value.values())) if isinstance(value, dict) else value
    return out


# ------------------------------------------------------------------ настройки и адрес


def test_disabled_means_no_target_and_a_readable_reason() -> None:
    with pytest.raises(ExportUnavailable) as boom:
        LangfuseTarget.from_settings(_settings(langfuse_enabled=False))
    assert "LANGFUSE_ENABLED" in str(boom.value)


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"langfuse_host": ""}, "LANGFUSE_HOST"),
        ({"langfuse_public_key": ""}, "LANGFUSE_PUBLIC_KEY"),
        ({"langfuse_secret_key": "  "}, "LANGFUSE_SECRET_KEY"),
    ],
)
def test_half_configured_is_refused_not_half_sent(over: dict[str, Any], needle: str) -> None:
    """«Хост есть, ключа нет» — это не «отправим как сможем»: часть данных утекла бы вникуда."""
    with pytest.raises(ExportUnavailable) as boom:
        LangfuseTarget.from_settings(_settings(**over))
    assert needle in str(boom.value)


def test_url_auth_and_ingestion_header() -> None:
    target = LangfuseTarget.from_settings(_settings())
    assert target.url == f"https://cloud.langfuse.com{OTLP_TRACES_PATH}"
    headers = target.headers
    assert headers["x-langfuse-ingestion-version"] == "4", (
        "без заголовка v4 задерживает до 10 минут"
    )
    import base64

    login = base64.b64decode(headers["Authorization"].removeprefix("Basic ")).decode()
    assert login == "pk-lf-1234567890:sk-lf-abcdefghij", "Basic-пару ключей менять нельзя"
    assert headers["Content-Type"] == "application/json"


def test_describe_never_leaks_the_secret() -> None:
    target = LangfuseTarget.from_settings(_settings())
    described = target.describe()
    assert "sk-lf" not in described and "abcdefghij" not in described
    assert "cloud.langfuse.com" in described


# ------------------------------------------------------------------ во что превращается журнал


def test_trace_becomes_root_span_plus_one_per_record() -> None:
    spans = spans_for_trace([_record(), _record(id="rec-2", kind="llm_call", model="glm-4-plus")])
    assert len(spans) == 3, "корень + по спану на запись"
    root, children = spans[0], spans[1:]
    assert root["name"] == "turn #3" and "parentSpanId" not in root
    attrs = _attrs(root)
    assert attrs["langfuse.trace.name"] == "aegis turn #3"
    assert attrs["langfuse.user.id"] == "7"
    assert attrs["langfuse.session.id"] == "owner-7", "ходы одного владельца должны группироваться"
    assert attrs["langfuse.trace.metadata.aegis.trace_id"] == TRACE
    assert all(child["parentSpanId"] == root["spanId"] for child in children)


def test_kinds_map_to_langfuse_observation_types() -> None:
    spans = spans_for_trace(
        [
            _record(id="a", kind="llm_call", model="glm-4-plus"),
            _record(id="b", kind="tool_run", params={"tool": "web_search", "trust": "untrusted"}),
            _record(id="c", kind="policy", policy={"decision": "deny", "reason": "подтверждение"}),
            _record(id="d", kind="verdict", policy={"severity": "critical"}),
            _record(id="e", kind="unknown_kind"),
        ]
    )[1:]
    types = [_attrs(span)["langfuse.observation.type"] for span in spans]
    assert types == ["generation", "tool", "guardrail", "evaluator", "span"]
    assert spans[1]["name"] == "tool_run" and spans[0]["name"] == "llm_call:glm-4-plus"


def test_policy_deny_and_failed_call_are_visible_as_errors() -> None:
    spans = spans_for_trace(
        [
            _record(id="a", kind="policy", policy={"decision": "deny"}),
            _record(id="b", kind="tool_run", params={"ok": False}, note="таймаут источника"),
        ]
    )[1:]
    denied, failed = (_attrs(span) for span in spans)
    assert denied["langfuse.observation.level"] == "ERROR"
    assert failed["langfuse.observation.level"] == "ERROR"
    assert failed["langfuse.observation.status_message"] == "таймаут источника"
    assert [span["status"]["code"] for span in spans] == [2, 2], "OTel status: 2 == ERROR"


def test_generation_carries_tokens_cost_and_prompt_refs() -> None:
    span = spans_for_trace(
        [
            _record(
                id="a",
                kind="llm_call",
                model="glm-4-plus",
                params={
                    "prompt_tokens": 1200,
                    "completion_tokens": 80,
                    "role": "brain",
                    "attempt": 1,
                },
                cost_usd=0.0042,
                prompt_ids=[{"id": "supervisor/system", "version": "0.7", "sha256": "ab" * 32}],
                latency_ms=2400,
            )
        ]
    )[1]
    attrs = _attrs(span)
    assert json.loads(attrs["langfuse.observation.usage_details"]) == {
        "input": 1200,
        "output": 80,
        "total": 1280,
    }
    assert json.loads(attrs["langfuse.observation.cost_details"]) == {
        "total": pytest.approx(0.0042)
    }
    assert attrs["langfuse.observation.model.name"] == "glm-4-plus"
    refs = json.loads(attrs["langfuse.observation.metadata.aegis.prompt_ids"])
    assert refs == [{"id": "supervisor/system", "version": "0.7", "sha256": "ab" * 32}]
    assert attrs["langfuse.observation.metadata.aegis.role"] == "brain"
    assert attrs["langfuse.observation.metadata.aegis.attempt"] == "1"
    nano_span = int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])
    assert nano_span == 2_400 * 1_000_000, "latency из журнала должна стать длительностью"


def test_content_is_clipped_and_marked_not_silently_short() -> None:
    long = "п" * 900
    span = spans_for_trace([_record(input_text=long, truncated=True)], max_chars=300)[1]
    attrs = _attrs(span)
    assert len(attrs["langfuse.observation.input"]) == 300
    assert attrs["langfuse.observation.input"].endswith("…")
    assert attrs["langfuse.observation.metadata.aegis.truncated"] is True, "срез != содержимое"


def test_clipped_json_never_leaves_as_broken_json() -> None:
    """Канонический JSON блобов ASCII-экранирован: срез посреди `\\u04` сломал бы просмотр.

    Вместо обрезанного документа витрина получает объект с превью — то, что данные урезаны,
    становится явным, а не «Langfuse почему-то не показывает содержимое».
    """
    payload = orjson.dumps({"text": "х" * 2_000}).decode()
    attrs = _attrs(spans_for_trace([_record(input_text=payload)], max_chars=200)[1])
    shipped = attrs["langfuse.observation.input"]
    parsed = json.loads(shipped)
    assert parsed["aegis_truncated_at_chars"] == 200 and parsed["preview"].endswith("…")


def test_short_json_is_passed_through_untouched() -> None:
    payload = '{"a": 1}'
    attrs = _attrs(spans_for_trace([_record(output_text=payload)], max_chars=200)[1])
    assert attrs["langfuse.observation.output"] == payload


def test_ids_are_deterministic_across_repeats() -> None:
    records = [_record(), _record(id="rec-2", kind="llm_call", model="m")]
    first = spans_for_trace(records)
    second = spans_for_trace([dict(item) for item in records])
    assert [span["spanId"] for span in first] == [span["spanId"] for span in second]
    assert len({span["spanId"] for span in first}) == len(first), "id спанов не должны совпадать"


def test_non_uuid_trace_still_produces_valid_ids() -> None:
    span = spans_for_trace([_record(trace_id="не uuid")])[0]
    assert len(span["traceId"]) == 32 and int(span["traceId"], 16) >= 0
    assert len(span["spanId"]) == 16


def test_empty_window_produces_no_spans() -> None:
    assert spans_for_trace([]) == []


# ------------------------------------------------------------------ конверт и пачки


def test_otlp_envelope_is_json_and_flattened() -> None:
    traces = [spans_for_trace([_record()]), spans_for_trace([_record(trace_id="b" * 32)])]
    payload = to_otlp(traces)
    body = orjson.loads(orjson.dumps(payload))
    spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 4, "трейсы едут одним запросом: трейс определяется полем traceId у спана"
    resource = body["resourceSpans"][0]["resource"]["attributes"]
    assert {"key": "service.name", "value": {"stringValue": "aegis"}} in resource


def test_batches_split_by_body_size_not_by_count() -> None:
    small = [{"traceId": "a" * 32, "spanId": "b" * 16, "attributes": [], "name": "x" * 400}]
    many = [[dict(item) for item in small] for _ in range(4)]
    assert _batches(many) == [many], "мелкие трейсы не надо резать"
    big = [[{"traceId": "a" * 32, "spanId": "b" * 16, "attributes": [], "name": "y" * 400_000}]]
    chunks = _batches(big * 6)
    assert len(chunks) > 1
    assert all(len(orjson.dumps(to_otlp(chunk))) <= MAX_REQUEST_BYTES for chunk in chunks[:-1])


def test_oversized_single_trace_goes_alone_instead_of_looping() -> None:
    huge = [[{"traceId": "a" * 32, "spanId": "b" * 16, "attributes": [], "name": "z" * 3_000_000}]]
    chunks = _batches(huge)
    assert len(chunks) == len(huge), "один спан больше лимита — это его проблема, а не тупик"


# ------------------------------------------------------------------ транспорт


def _client(responses: list[tuple[int, str]], seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, text = responses[min(len(seen) - 1, len(responses) - 1)]
        if status == 0:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(status, text=text)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_send_accepts_multistatus_and_sends_auth() -> None:
    seen: list[httpx.Request] = []
    target = LangfuseTarget.from_settings(_settings())
    async with _client([(207, '{"successes":[],"errors":[]}')], seen) as client:
        await send({"resourceSpans": []}, target, client=client)
    (request,) = seen
    assert request.headers["authorization"].startswith("Basic ")
    assert request.headers["x-langfuse-ingestion-version"] == "4"
    assert orjson.loads(request.content) == {"resourceSpans": []}


async def test_send_reports_4xx_instead_of_retrying_silently() -> None:
    seen: list[httpx.Request] = []
    target = LangfuseTarget.from_settings(_settings())
    async with _client([(401, "invalid credentials")], seen) as client:
        with pytest.raises(ExportFailed) as boom:
            await send({"resourceSpans": []}, target, client=client)
    assert "401" in str(boom.value) and len(seen) == 1, "повторов на 401 быть не должно"


async def test_network_error_is_export_failed_not_a_traceback() -> None:
    target = LangfuseTarget.from_settings(_settings())
    async with _client([(0, "")], []) as client:
        with pytest.raises(ExportFailed) as boom:
            await send({"resourceSpans": []}, target, client=client)
    assert "не отвечает" in str(boom.value)


# ------------------------------------------------------------------ прогон окна


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeSession:
    """Минимальная замена session(): журнал читается один запросом, и `factory()` обязан вызываться.

    Класс играет и фабрику, и контекст-менеджер — ровно как `aegis.platform.db.session`.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls = 0

    def __call__(self) -> _FakeSession:
        return self

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def execute(self, _stmt: Any) -> _FakeResult:
        self.calls += 1
        return _FakeResult(self.rows)


async def test_collect_decodes_blobs_and_groups_by_trace() -> None:
    rows = [
        _record(input_content=b'{"messages": "x"}', output_content=b'{"content": "\xd0\xba"}'),
        _record(id="rec-2", seq=11, kind="llm_call", model="m"),
    ]
    factory = _FakeSession(rows)
    traces = await collect(session_factory=factory, max_chars=5_000)
    assert len(traces) == 1 and len(traces[0]) == 3
    child = _attrs(traces[0][1])
    assert json.loads(child["langfuse.observation.input"]) == {"messages": "x"}
    assert child["langfuse.observation.output"] == '{"content": "к"}'
    assert factory.calls == 1, "окно читается один запросом, а не по трейсу"


async def test_collect_keeps_the_full_text_and_clipping_happens_once() -> None:
    """Двойного среза быть не должно: один раз — при сборке атрибута, и больше нигде."""
    rows = [_record(output_content=b"q" * 5_000)]
    traces = await collect(session_factory=_FakeSession(rows), max_chars=120)
    attrs = _attrs(traces[0][1])
    assert len(attrs["langfuse.observation.output"]) == 120
    assert attrs["langfuse.observation.output"].endswith("…")


async def test_export_window_sends_every_batch_and_counts() -> None:
    seen: list[httpx.Request] = []
    rows = [_record(), _record(id="rec-2", seq=11, kind="llm_call", model="m")]
    target = LangfuseTarget.from_settings(_settings())
    async with _client([(207, "{}")], seen) as client:
        report = await export_window(
            target=target,
            session_factory=_FakeSession(rows),
            client=client,
            cfg=_settings(),
        )
    assert report.ok and report.traces == 1 and report.spans == 3 and report.batches == 1
    assert report.summary() == "3 спана в 1 трейсе · отправлено 1 запрос"
    body = orjson.loads(seen[0].content)
    assert len(body["resourceSpans"][0]["scopeSpans"][0]["spans"]) == 3


async def test_export_window_stops_at_first_refusal_and_reports_it() -> None:
    seen: list[httpx.Request] = []
    wide = [_record(id=f"rec-{index}", seq=index) for index in range(2)]
    target = LangfuseTarget.from_settings(_settings())
    async with _client([(500, "boom")], seen) as client:
        report = await export_window(
            target=target,
            session_factory=_FakeSession(wide),
            client=client,
            cfg=_settings(),
        )
    assert not report.ok and report.batches == 0
    assert "500" in report.summary() and "500" in report.stopped


async def test_dry_run_counts_without_a_single_request() -> None:
    seen: list[httpx.Request] = []
    target = LangfuseTarget.from_settings(_settings())
    async with _client([(200, "{}")], seen) as client:
        report = await export_window(
            target=target,
            session_factory=_FakeSession([_record()]),
            client=client,
            dry_run=True,
            cfg=_settings(),
        )
    assert seen == [], "проба не имеет права ничего отправлять"
    assert report.dry_run and report.traces == 1
    assert report.summary() == "ушли бы 2 спана в 1 трейсе · 1 запрос"


async def test_repeat_of_the_same_window_sends_the_same_body() -> None:
    """Главное свойство «без состояния»: то же окно = тот же запрос, значит дублей не будет."""
    rows = [_record(), _record(id="rec-2", seq=11, kind="tool_run", params={"tool": "t"})]
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(207, text="{}")

    target = LangfuseTarget.from_settings(_settings())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(2):
            await export_window(
                target=target, session_factory=_FakeSession(rows), client=client, cfg=_settings()
            )
    assert len(bodies) == 2 and bodies[0] == bodies[1]


def test_export_unavailable_is_not_export_failed() -> None:
    """Два отказа, две судьбы: «настраивай окружение» и «витрина не приняла»."""
    assert not issubclass(ExportUnavailable, ExportFailed)
    assert not issubclass(ExportFailed, ExportUnavailable)
