"""Стриминг в шлюзе: склейка чанков, фрагменты tool_calls, обрывы и usage.

Как и в `test_gateway`, подменён только сетевой клиент. Накопитель, DLP, стоимость и журнал —
настоящие: ровно на их стыке со стримингом и теряются ответы («не тот ценник», «битый JSON
аргументов», «половина ответа как целый»).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import APIStatusError

from aegis.platform.gateway.client import ModelUnavailable
from aegis.platform.gateway.streaming import StreamCollector
from conftest import api_error, make_gateway, response


class FakeStream:
    """`async for` по чанкам — то, что отдаёт openai-клиент при `stream=True`.

    Элемент-исключение бросается на своём месте, а не до потока: так проверяется обрыв посреди
    ответа, где «повторить запрос» уже означает второй ответ у владельца на экране.
    """

    def __init__(self, chunks: list[Any]) -> None:
        self.chunks = list(chunks)

    async def __aiter__(self) -> Any:
        for item in self.chunks:
            if isinstance(item, BaseException):
                raise item
            await asyncio.sleep(0)
            yield item


def delta_chunk(
    content: str | None = None,
    *,
    tool_calls: list[Any] | None = None,
    reasoning: str | None = None,
    finish: str | None = None,
    model: str | None = "glm-test",
) -> SimpleNamespace:
    delta = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=reasoning)
    return SimpleNamespace(model=model, usage=None, choices=[_choice(delta, finish)])


def _choice(delta: Any, finish: str | None) -> SimpleNamespace:
    return SimpleNamespace(delta=delta, finish_reason=finish)


def tool_delta(
    index: int, *, call_id: str | None = None, name: str | None = None, arguments: str | None = None
) -> list[SimpleNamespace]:
    frag = SimpleNamespace(
        index=index, id=call_id, function=SimpleNamespace(name=name, arguments=arguments)
    )
    return [frag]


def usage_chunk(prompt: int = 1000, completion: int = 500) -> SimpleNamespace:
    """Последний чанк без `choices`: на нём `choices[0]` и падают реализации в лоб."""
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, total_tokens=0)
    return SimpleNamespace(model="glm-test", usage=usage, choices=[])


def bad_request(text: str) -> APIStatusError:
    request = httpx.Request("POST", "https://api.example/v1/chat/completions")
    return APIStatusError(text, response=httpx.Response(400, request=request), body=None)


def sink() -> tuple[list[str], Any]:
    seen: list[str] = []

    async def on_text(piece: str) -> None:
        seen.append(piece)

    return seen, on_text


# ------------------------------------------------------------------ накопитель


async def test_stream_text_is_glued_and_reported_incrementally() -> None:
    seen, on_text = sink()
    gateway, _kv, _primary, records, _meta = make_gateway(
        [
            FakeStream(
                [
                    delta_chunk("При"),
                    delta_chunk("вет"),
                    delta_chunk(", ок", finish="stop"),
                    usage_chunk(prompt=300, completion=70),
                ]
            )
        ]
    )
    result = await gateway.chat_stream(
        "brain", [{"role": "user", "content": "привет"}], on_text=on_text
    )

    assert seen == ["При", "вет", ", ок"], "владельцу должны уйти ровно те же куски, что пришли"
    assert result.content == "Привет, ок"
    assert result.truncated is False
    assert result.prompt_tokens == 300 and result.completion_tokens == 70
    assert records[-1].ok is True and records[-1].completion_tokens == 70


async def test_streaming_is_priced_by_usage_not_by_estimate() -> None:
    from aegis.platform.gateway.models import CATALOG

    gateway, _kv, primary, records, _meta = make_gateway(
        [FakeStream([delta_chunk("ок", finish="stop"), usage_chunk(prompt=1000, completion=500)])]
    )
    result = await gateway.chat_stream("brain", [{"role": "user", "content": "длинный" * 50}])

    sent = primary.requests[0]
    assert sent["stream"] is True
    assert sent["stream_options"] == {"include_usage": True}
    expected = (
        1000 * CATALOG["brain"].in_usd_per_m + 500 * CATALOG["brain"].out_usd_per_m
    ) / 1_000_000
    assert result.cost_usd == pytest.approx(expected, abs=1e-9)
    assert records[-1].cost_usd == pytest.approx(expected, abs=1e-9)


async def test_streamed_and_plain_answers_are_the_same_result() -> None:
    """Инвариант «одна реализация»: `ChatResult` из стрима не должен отличаться по форме.

    Если здесь появится расхождение, то и весь остальной слой (история, tools, журнал) начнёт жить
    двумя путями — а двух путей у нас нет по определению.
    """
    plain, _kv1, _p1, _r1, _m1 = make_gateway([response("ок")])
    streamed, _kv2, _p2, _r2, _m2 = make_gateway(
        [FakeStream([delta_chunk("ок", finish="stop"), usage_chunk()])]
    )
    a = await plain.chat("brain", [{"role": "user", "content": "go"}])
    b = await streamed.chat_stream("brain", [{"role": "user", "content": "go"}])

    assert b.raw_message == a.raw_message
    assert (b.content, b.model, b.cost_usd) == (a.content, a.model, a.cost_usd)
    assert b.tool_calls == [] == a.tool_calls


async def test_reasoning_is_not_shown_but_is_journalled() -> None:
    seen, on_text = sink()
    gateway, _kv, _primary, _records, _meta = make_gateway(
        [
            FakeStream(
                [
                    delta_chunk(None, reasoning="думал"),
                    delta_chunk("ответ", finish="stop"),
                ]
            )
        ]
    )
    result = await gateway.chat_stream(
        "brain", [{"role": "user", "content": "go"}], on_text=on_text
    )

    assert seen == ["ответ"], "черновик рассуждений — не часть ответа владельцу"
    assert result.raw_message["reasoning_content"] == "думал"


# ------------------------------------------------------------------ tools


async def test_tool_call_fragments_become_valid_arguments() -> None:
    gateway, _kv, _primary, _records, _meta = make_gateway(
        [
            FakeStream(
                [
                    delta_chunk(None, tool_calls=tool_delta(0, call_id="call_1", name="note_add")),
                    delta_chunk(None, tool_calls=tool_delta(0, arguments='{"text":')),
                    delta_chunk(None, tool_calls=tool_delta(0, arguments='"заметка"}')),
                    delta_chunk(None, finish="tool_calls"),
                    usage_chunk(),
                ]
            )
        ]
    )
    result = await gateway.chat_stream("brain", [{"role": "user", "content": "заметку"}])

    call = result.tool_calls[0]
    assert call.id == "call_1" and call.name == "note_add"
    assert json.loads(call.arguments) == {"text": "заметка"}
    raw = result.raw_message["tool_calls"][0]
    assert raw["type"] == "function" and raw["function"]["name"] == "note_add"
    assert result.content is None, "ответ без текста — это вызов инструмента, а не пустой ответ"


async def test_two_tool_calls_by_index_do_not_merge() -> None:
    gateway, _kv, _primary, _records, _meta = make_gateway(
        [
            FakeStream(
                [
                    delta_chunk(
                        None,
                        tool_calls=tool_delta(0, call_id="a", name="one")
                        + tool_delta(1, call_id="b", name="two"),
                    ),
                    delta_chunk(None, tool_calls=tool_delta(1, arguments="{}")),
                    delta_chunk(None, tool_calls=tool_delta(0, arguments="{}")),
                    delta_chunk(None, finish="tool_calls"),
                ]
            )
        ]
    )
    result = await gateway.chat_stream("brain", [{"role": "user", "content": "два"}])

    assert [c.name for c in result.tool_calls] == ["one", "two"]
    assert all(c.arguments == "{}" for c in result.tool_calls)


# ------------------------------------------------------------------ обрывы


async def test_break_after_first_delta_keeps_partial_and_does_not_retry() -> None:
    seen, on_text = sink()
    gateway, _kv, primary, records, _meta = make_gateway(
        [FakeStream([delta_chunk("начало"), RuntimeError("socket closed")])]
    )
    result = await gateway.chat_stream(
        "brain", [{"role": "user", "content": "go"}], on_text=on_text
    )

    assert result.content == "начало"
    snap = records[-1].response or {}
    assert snap["streamed"] is True and snap["interrupted"] is True
    # форма журнала одна для обоих путей: «что увидел агент» описывается снапшотом сообщения
    assert snap["choices"][0]["message"]["content"] == "начало"
    assert result.truncated is True, (
        "неполный ответ обязан быть помечен: иначе это «ответ как есть»"
    )
    assert len(primary.requests) == 1, "повтор после показанного текста дал бы второй ответ"
    assert seen == ["начало"]
    assert records[-1].ok is True and "оборвался" in (records[-1].error or "")


async def test_break_before_any_text_is_retried_like_usual() -> None:
    gateway, _kv, primary, records, _meta = make_gateway(
        [FakeStream([api_error(429)]), FakeStream([delta_chunk("целиком")])]
    )
    result = await gateway.chat_stream("brain", [{"role": "user", "content": "go"}])

    assert result.content == "целиком"
    assert result.truncated is False
    assert len(primary.requests) == 2
    assert [r.ok for r in records] == [False, True]


async def test_all_providers_broken_still_raises_model_unavailable() -> None:
    gateway, _kv, _primary, records, _meta = make_gateway([FakeStream([RuntimeError("boom")])])
    with pytest.raises(ModelUnavailable):
        await gateway.chat_stream("brain", [{"role": "user", "content": "go"}])
    assert records and records[-1].ok is False


async def test_finish_reason_length_marks_truncation_without_error_note() -> None:
    gateway, _kv, _primary, records, _meta = make_gateway(
        [FakeStream([delta_chunk("обрезано по max_tokens", finish="length")])]
    )
    result = await gateway.chat_stream("brain", [{"role": "user", "content": "go"}], max_tokens=8)

    assert result.truncated is True
    assert records[-1].error is None, "лимит токенов — не сбой канала"


# ------------------------------------------------------------------ DLP и провайдеры


async def test_dlp_token_split_between_chunks_is_still_restored() -> None:
    gateway, _kv, primary, _records, _meta = make_gateway(
        [
            FakeStream(
                [
                    delta_chunk("карта "),
                    delta_chunk("<AEGIS_PII:"),
                    delta_chunk("CARD:1>"),
                ]
            )
        ]
    )
    result = await gateway.chat_stream(
        "brain", [{"role": "user", "content": "сохрани карту 2202 2036 5151 2225"}]
    )

    assert "2202" not in str(primary.requests[0]["messages"])
    assert result.content == "карта 2202 2036 5151 2225", "склеиваем текст, а не маскировку"


async def test_provider_without_stream_options_is_asked_again_without_the_field() -> None:
    gateway, _kv, primary, records, _meta = make_gateway(
        [
            bad_request("400 Invalid request: unknown parameter 'stream_options'"),
            FakeStream([delta_chunk("ок", finish="stop")]),
        ]
    )
    result = await gateway.chat_stream("brain", [{"role": "user", "content": "go"}])

    assert result.content == "ок"
    assert "stream_options" in primary.requests[0]
    assert "stream_options" not in primary.requests[1]
    assert primary.requests[1]["stream"] is True, "стриминг остаётся, уходит только поле про usage"
    assert [r.ok for r in records] == [False, True]


async def test_usage_estimate_when_provider_sends_no_usage() -> None:
    gateway, _kv, _primary, records, _meta = make_gateway(
        [FakeStream([delta_chunk("ответ на десять букв", finish="stop")])]
    )
    result = await gateway.chat_stream("brain", [{"role": "user", "content": "вопрос"}])

    assert result.content == "ответ на десять букв"
    # без usage-чанка остаются наши оценки по длине: важно, что они ненулевые — иначе /cost
    # показывал бы ноль для всех стриминговых ответов, и бюджет стал бы фиктивным
    assert records[-1].completion_tokens == int(len("ответ на десять букв") / 3.5)
    assert records[-1].prompt_tokens == int(len("вопрос") / 3.5)


# ------------------------------------------------------------------ коллектор напрямую


async def test_collector_survives_chunks_without_delta_and_choices() -> None:
    collector = StreamCollector()
    await collector.collect(
        FakeStream(
            [
                SimpleNamespace(model=None, usage=None, choices=[]),
                SimpleNamespace(model="m", usage=None, choices=[_choice(None, None)]),
                delta_chunk("текст"),
                usage_chunk(prompt=10, completion=2),
            ]
        )
    )
    got = collector.result()

    assert got.content == "текст"
    assert got.prompt_tokens == 10 and got.model == "m"
    assert got.finish_reason is None
    assert collector.has_text is True
