"""Связность M1: снимки полезной нагрузки в шлюзе, веер записей, разбор хода и replay.

Без БД: проверяем, что *данные уходят куда надо* и что replay опирается на записанный вход, а не
на «то, что модель помнит». SQL-часть — в ``tests/integration/test_repro.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from aegis.governance.replay import (
    ReplayJudgement,
    ReplayReport,
    replay_trace,
    trim_to_frozen_world,
)
from aegis.platform.gateway.client import ChatResult, LLMCallRecord, ModelUnavailable, extract_json
from aegis.runtime import _make_recorder
from conftest import api_error, make_gateway, response


class Verdict(BaseModel):
    equivalent: bool
    score: float = 0.0


def _chat_result(content: str) -> ChatResult:
    return ChatResult(
        content=content,
        tool_calls=[],
        model="glm-4.7",
        role="fast",
        prompt_tokens=5,
        completion_tokens=7,
        cost_usd=0.001,
        latency_ms=20,
        raw_message={},
    )


# ------------------------------------------------------------------ extract_json


def test_extract_json_accepts_fences_and_prose() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Вот ответ:\n{"a": {"b": 2}}\nНадеюсь, помог.') == {"a": {"b": 2}}


def test_extract_json_refuses_invented_data() -> None:
    """«Не разобрали» лучше, чем «разобрали по-своему»: молчаливая догадка оседает в журнале."""
    with pytest.raises(ValueError, match="нет JSON-объекта"):
        extract_json("я не умею в JSON")


# ------------------------------------------------------------------ chat_json


async def test_chat_json_validates_and_retries_once() -> None:
    gateway, _kv, completions, _records, _meta = make_gateway(
        [
            response("извини, без схемы"),
            response('```json\n{"equivalent": true, "score": 0.9}\n```'),
        ]
    )
    result = await gateway.chat_json("fast", [{"role": "user", "content": "сравни"}], Verdict)
    assert result.equivalent is True and result.score == pytest.approx(0.9)
    assert len(completions.requests) == 2
    second = completions.requests[1]["messages"]
    assert second[-2]["content"] == "извини, без схемы"
    assert "не проходит схему" in second[-1]["content"]


async def test_chat_json_gives_up_after_the_retry() -> None:
    gateway, _kv, completions, _records, _meta = make_gateway(
        [response("не JSON"), response("снова не JSON")]
    )
    with pytest.raises(ModelUnavailable, match="не вернула валидный JSON"):
        await gateway.chat_json("fast", [{"role": "user", "content": "сравни"}], Verdict)
    assert len(completions.requests) == 2, "больше двух попыток — это уже жог бюджета"


async def test_chat_json_asks_the_api_for_json() -> None:
    gateway, _kv, completions, _records, _meta = make_gateway([response('{"equivalent": true}')])
    await gateway.chat_json("fast", [{"role": "user", "content": "сравни"}], Verdict)
    assert completions.requests[0]["response_format"] == {"type": "json_object"}


async def test_chat_json_reports_invalid_shape_as_error_not_none() -> None:
    """Схема строгая: ``{"score": 5}`` без ``equivalent`` — провал, а не «False по умолчанию»."""
    gateway, _kv, _c, _r, _m = make_gateway(
        [response('{"score": 5.0}'), response("по-прежнему не схема")]
    )
    with pytest.raises(ModelUnavailable):
        await gateway.chat_json("fast", [{"role": "user", "content": "сравни"}], Verdict)


# ------------------------------------------------------- снимки запроса/ответа


async def test_payload_snapshots_reach_the_recorder() -> None:
    gateway, _kv, _completions, _records, _meta = make_gateway(
        [response("ок", prompt_tokens=10, completion_tokens=5)]
    )
    captured: list[LLMCallRecord] = []

    async def sink(record: LLMCallRecord) -> None:
        captured.append(record)

    gateway.recorder = sink
    await gateway.chat(
        "brain", [{"role": "user", "content": "вопрос без PII"}], tools=None, temperature=0.4
    )
    assert len(captured) == 1
    call = captured[0]
    assert call.request is not None and call.request["temperature"] == 0.4
    assert call.request["messages"][-1]["content"] == "вопрос без PII"
    assert call.response is not None
    assert call.response["choices"][0]["message"]["content"] == "ок"


async def test_snapshots_are_skipped_when_the_journal_is_off() -> None:
    from aegis.platform.config import Settings

    cfg = Settings(
        _env_file=None,
        _env_prefix="T_",
        glm_api_key="k",
        llm_backoff_s=0.05,
        repro_record_payload=False,
    )
    gateway = make_gateway([response("ок")], cfg=cfg)[0]
    captured: list[LLMCallRecord] = []

    async def sink(record: LLMCallRecord) -> None:
        captured.append(record)

    gateway.recorder = sink
    await gateway.chat("fast", [{"role": "user", "content": "привет"}])
    assert captured[0].request is None and captured[0].response is None
    assert captured[0].prompt_tokens > 0, "метрики пишутся независимо от журнала содержимого"


async def test_failed_attempt_keeps_the_request_snapshot() -> None:
    """Снимок нужен и на провале: именно так выясняется, каким телом мы получили 400."""
    gateway = make_gateway([api_error(400)])[0]
    captured: list[LLMCallRecord] = []

    async def sink(record: LLMCallRecord) -> None:
        captured.append(record)

    gateway.recorder = sink
    with pytest.raises(ModelUnavailable):
        await gateway.chat("fast", [{"role": "user", "content": "плохой запрос"}])
    assert captured, "провал без записи — это невидимый провал"
    assert captured[0].request is not None
    assert captured[0].ok is False


# ------------------------------------------------------------------ веер записей


class Sink:
    def __init__(self) -> None:
        self.seen: list[Any] = []

    async def llm_call(self, record: Any) -> None:
        self.seen.append(record)

    async def on_llm_call(self, record: Any) -> None:
        self.seen.append(record)


async def test_gateway_hook_fans_out_to_audit_and_journal() -> None:
    """Один хук шлюза, два получателя: метрики и содержимое пишутся с одного вызова."""
    audit, journal = Sink(), Sink()
    record = LLMCallRecord(
        call_id="c",
        trace_id="t",
        role="brain",
        model="glm",
        prompt_tokens=1,
        completion_tokens=2,
        cost_usd=0.0,
        latency_ms=3,
        ok=True,
    )
    await _make_recorder(audit, journal)(record)  # type: ignore[arg-type]
    assert audit.seen == [record] and journal.seen == [record]


# ------------------------------------------------------------------ replay


class StubRecorder:
    """Журнал из памяти: ровно тот интерфейс, что нужен replay (записи + вход по ссылке)."""

    enabled = True
    failures = 0

    def __init__(self, records: list[dict[str, Any]], input_value: Any) -> None:
        self._records = records
        self._input = input_value

    async def records_for_trace(self, trace_id: str) -> list[dict[str, Any]]:
        return self._records

    async def record_input(self, record: dict[str, Any]) -> Any:
        return self._input


class StubGateway:
    def __init__(self, answer: str, judgement: ReplayJudgement | None) -> None:
        self.answer = answer
        self.judgement = judgement
        self.chat_calls: list[dict[str, Any]] = []
        self.judge_calls: list[dict[str, Any]] = []

    async def chat(self, role: str, messages: Any, **kwargs: Any) -> ChatResult:
        self.chat_calls.append({"role": role, "messages": messages, **kwargs})
        return _chat_result(self.answer)

    async def chat_json(self, role: str, messages: Any, schema: Any, **kwargs: Any) -> Any:
        self.judge_calls.append({"role": role, "messages": messages, **kwargs})
        if self.judgement is None:
            raise ValueError("судья молчит")
        return self.judgement


def _turn_record(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "turn_summary",
        "turn_no": 1,
        "model": "glm-4.7",
        "output": "исходный ответ",
        "params": {"iterations": 2, "route": "brain:tools"},
        "prompt_ids": [{"id": "core/system", "version": "sys-v0.3.0"}],
        "truncated": False,
        "input_sha": b"\x01" * 32,
    }
    return {**base, **over}


_MESSAGES = [
    {"role": "system", "content": "правила"},
    {"role": "user", "content": "вопрос"},
    {"role": "assistant", "content": "исходный ответ"},
]


async def test_replay_feeds_the_frozen_world_without_the_last_answer() -> None:
    gateway = StubGateway("исходный ответ", ReplayJudgement(equivalent=True, score=0.95))
    report = await replay_trace(
        "t",
        recorder=StubRecorder([_turn_record()], list(_MESSAGES)),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
    )
    assert report.ok and report.equivalent is True and report.judged
    assert report.dropped_messages == 1
    sent = gateway.chat_calls[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user"], "хвостового ответа входе быть не должно"
    assert gateway.chat_calls[0]["tools"] is None, (
        "замороженный мир: инструменты не перевыполняются"
    )
    assert report.cost_usd == pytest.approx(0.001)


async def test_replay_uses_the_verdict_of_the_prompted_judge() -> None:
    """Промпт судьи грузится файлом: проверяем, что плейсхолдеры реально подставлены."""
    gateway = StubGateway("то же", ReplayJudgement(equivalent=True, score=1.0))
    await replay_trace(
        "t",
        recorder=StubRecorder([_turn_record()], list(_MESSAGES)),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
    )
    prompt = gateway.judge_calls[0]["messages"][0]["content"]
    assert "исходный ответ" in prompt and "то же" in prompt
    assert "{{original}}" not in prompt, "неподставленный плейсхолдер ушёл бы в модель текстом"


async def test_replay_reports_model_change_without_hiding_it() -> None:
    gateway = StubGateway(
        "другое",
        ReplayJudgement(equivalent=False, score=0.3, critical=True, differences=["разные суммы"]),
    )
    report = await replay_trace(
        "t",
        recorder=StubRecorder([_turn_record(model="glm-4.6")], list(_MESSAGES)),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
    )
    assert report.model_changed is True
    text = report.as_text()
    assert "модель сменилась" in text and "НЕ эквивалентно" in text and "существенное" in text
    assert "разные суммы" in text


async def test_replay_says_when_the_model_is_unavailable() -> None:
    class DeadGateway(StubGateway):
        async def chat(self, role: str, messages: Any, **kwargs: Any) -> ChatResult:
            raise ModelUnavailable("провайдер не отвечает", cause="429")

    report = await replay_trace(
        "t",
        recorder=StubRecorder([_turn_record()], list(_MESSAGES)),  # type: ignore[arg-type]
        gateway=DeadGateway("x", None),  # type: ignore[arg-type]
    )
    assert not report.ok and "недоступна" in report.reason


async def test_replay_is_honest_when_input_is_lost() -> None:
    report = await replay_trace(
        "t",
        recorder=StubRecorder([_turn_record()], None),  # type: ignore[arg-type]
        gateway=StubGateway("x", None),  # type: ignore[arg-type]
    )
    assert not report.ok and "утрачен" in report.reason


async def test_replay_survives_a_silent_judge() -> None:
    """Судья — опция: без него отчёт обязан показать, что именно не смог, а не «ошибка»."""
    gateway = StubGateway("то же самое", None)
    report = await replay_trace(
        "t",
        recorder=StubRecorder([_turn_record()], list(_MESSAGES)),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
    )
    assert report.ok and report.judged is False
    assert "судья не отвечал" in report.as_text()


async def test_replay_without_any_records_says_so() -> None:
    report = await replay_trace(
        "t",
        recorder=StubRecorder([], None),  # type: ignore[arg-type]
        gateway=StubGateway("x", None),  # type: ignore[arg-type]
    )
    assert not report.ok and "записей нет" in report.reason


def test_trim_only_removes_a_trailing_answer() -> None:
    with_tools = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "данные"},
    ]
    trimmed, dropped = trim_to_frozen_world(with_tools)
    assert dropped == 0 and trimmed == with_tools

    answered = [*with_tools, {"role": "assistant", "content": "итог"}]
    trimmed, dropped = trim_to_frozen_world(answered)
    assert dropped == 1 and trimmed == with_tools


def test_failed_report_text_names_the_reason() -> None:
    report = ReplayReport(trace_id="abc", ok=False, reason="нет turn_summary")
    assert "не удалось" in report.as_text() and "нет turn_summary" in report.as_text()
