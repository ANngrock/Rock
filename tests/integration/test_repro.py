"""Журнал решений на живом Postgres: цепочка, триггеры, блобы, якорь, усечение.

Юнит-тесты проверяют арифметику хэшей; здесь проверяется SQL — тот, который моки не видят:
``INSTEAD OF``-триггеры, ``jsonb``-раскрытие, ``octet_length``-чеки, advisory lock под параллельной
записью и усечение блобов лимитом. Именно из-за отсутствия этого файла в офлайне однажды «прошёл»
запрос с пропущенной запятой.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import orjson
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

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
    """Настройки рекордера внутри override_settings: конфиг читается явно, а не «как получится».

    ``_env_file=None`` обязателен: иначе локальный ``.env`` владельца просачивается в интеграции и
    «падает только у меня» становится рабочим режимом.
    """
    return Settings(_env_file=None, _env_prefix="T_", glm_api_key="k", **over)  # type: ignore[call-arg]


async def _one_turn(recorder: SqlDecisionRecorder, owner_id: int = 777) -> str:
    trace = str(uuid.uuid4())
    prompts = [{"id": "core/system", "version": "sys-v0.3.0", "sha256": "a" * 64}]
    recorder.begin_turn(trace, owner_id=owner_id, prompt_ids=prompts, tools_schema_sha=b"\x01" * 32)
    assert recorder.turn_step(trace) == 1, "шаги с единицы: 0 значит «ход не размечен»"
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
            request={"model": "glm-4.7", "messages": [{"role": "user", "content": "привет"}]},
            response={"model": "glm-4.7", "choices": [{"message": {"content": "ок"}}]},
        )
    )
    await recorder.policy(
        trace_id=trace,
        turn_no=1,
        owner_id=owner_id,
        tool="pay",
        decision="confirm",
        reason="платёж требует подтверждения",
        risk="high",
    )
    await recorder.tool_run(
        trace_id=trace,
        turn_no=2,
        owner_id=owner_id,
        tool="pay",
        args={"sum": 100},
        result="переведено",
        trust="system",
        decision="confirmed",
        ok=True,
        latency_ms=15,
    )
    await recorder.turn_summary(
        trace_id=trace,
        turn_no=2,
        owner_id=owner_id,
        messages=[
            {"role": "system", "content": "правила"},
            {"role": "user", "content": "привет"},
            {"role": "assistant", "content": "ок"},
        ],
        answer="ок",
        model="glm-4.7",
        cost_usd=0.00123,
        latency_ms=900,
        iterations=2,
        prompt_ids=prompts,
        tools_schema_sha=b"\x01" * 32,
        route="brain:tools",
        notes=["SearXNG не ответил"],
    )
    recorder.end_turn(trace)
    return trace


@pytest.mark.usefixtures("db")
async def test_turn_lands_in_the_chain_and_reads_back() -> None:
    recorder = SqlDecisionRecorder(_cfg())
    failures_before = recorder.failures
    trace = await _one_turn(recorder)
    assert failures_before == 0 and recorder.failures == 0, "рекордер глотает ошибки — надо видеть"

    records = await recorder.records_for_trace(trace)
    kinds = [row["kind"] for row in records]
    assert kinds == ["llm_call", "policy", "tool_run", "turn_summary"]

    turn = records[-1]
    assert turn["model"] == "glm-4.7"
    assert turn["prompt_ids"][0]["version"] == "sys-v0.3.0"
    assert turn["policy"] is None and turn["truncated"] is False
    assert "SearXNG" in str(turn["note"])

    messages = orjson.loads(turn["input"])
    assert [m["role"] for m in messages] == ["system", "user", "assistant"]
    assert turn["output"] == "ок"

    llm = records[0]
    assert llm["params"]["prompt_tokens"] == 120 and llm["params"]["ok"] is True
    assert llm["params"]["role"] == "brain" and llm["turn_no"] == 1
    assert str(llm["cost_usd"]) == "0.001230"
    # содержимое llm_call — это снимки запроса/ответа: input = то, что ушло в API, output = ответ
    request = orjson.loads(llm["input"])
    assert request["messages"][0]["content"] == "привет"
    assert request["model"] == "glm-4.7"
    assert orjson.loads(llm["output"])["choices"][0]["message"]["content"] == "ок"


@pytest.mark.usefixtures("db")
async def test_chain_verifies_and_stats_are_real() -> None:
    recorder = SqlDecisionRecorder(_cfg())
    await _one_turn(recorder)
    report = await recorder.verify(limit=100_000)
    assert report.ok, f"цепочка разорвана своими же записями: {report.problems}"
    assert report.checked >= 4 and report.gaps == 0
    stats = await recorder.stats()
    assert stats["enabled"] is True
    assert stats["records"] >= 4 and stats["blobs"] >= 2 and stats["blob_bytes"] > 0
    assert stats["failures"] == 0


@pytest.mark.usefixtures("db")
async def test_parallel_appends_keep_the_chain_linear() -> None:
    """advisory lock на вставку: без него два хода дадут одну prev_hash — дерево вместо журнала."""
    recorder = SqlDecisionRecorder(_cfg())
    traces = [uuid.uuid4() for _ in range(12)]

    async def write(trace: uuid.UUID, index: int) -> None:
        await recorder.policy(
            trace_id=str(trace),
            turn_no=1,
            owner_id=800 + index,
            tool="x",
            decision="allow",
            reason=f"гонка {index}",
            risk="low",
        )

    await asyncio.gather(*(write(trace, i) for i, trace in enumerate(traces)))
    report = await recorder.verify(limit=200_000)
    assert report.ok, report.problems
    assert report.gaps == 0


@pytest.mark.usefixtures("db")
async def test_records_are_append_only_and_blobs_are_content_addressed() -> None:
    """Триггеры — не украшение: без них «неизменяемый журнал» правится одним UPDATE."""
    recorder = SqlDecisionRecorder(_cfg())
    trace = await _one_turn(recorder)
    async with session() as s:
        row = (
            (
                await s.execute(
                    text(
                        "SELECT id, input_sha FROM governance.decision_records "
                        "WHERE trace_id = CAST(:t AS uuid) AND kind = 'turn_summary'"
                    ),
                    {"t": trace},
                )
            )
            .mappings()
            .one()
        )

        with pytest.raises(Exception, match="append-only"):
            await s.execute(
                text(
                    "UPDATE governance.decision_records SET note = 'подмена' "
                    "WHERE id = CAST(:i AS uuid)"
                ),
                {"i": str(row["id"])},
            )
    # транзакция после ошибки испорчена — проверяем DELETE отдельной сессией
    async with session() as s:
        with pytest.raises(DBAPIError, match="append-only"):
            await s.execute(
                text("DELETE FROM governance.decision_records WHERE id = CAST(:i AS uuid)"),
                {"i": str(row["id"])},
            )

    async with session() as s:
        with pytest.raises(DBAPIError, match="append-only"):
            await s.execute(
                text("UPDATE platform.blobs SET size_bytes = 1 WHERE sha256 = :sha"),
                {"sha": row["input_sha"]},
            )


@pytest.mark.usefixtures("db")
async def test_oversized_content_is_truncated_and_says_so() -> None:
    """Усечение — отметка в записи, а не тихая обрезка: иначе replay «не сходится» вот так."""
    recorder = SqlDecisionRecorder(_cfg(repro_max_blob_bytes=4096))
    trace = str(uuid.uuid4())
    big = "д" * 9000
    await recorder.turn_summary(
        trace_id=trace,
        turn_no=1,
        owner_id=778,
        messages=[{"role": "user", "content": big}],
        answer=big,
        model="glm-4.7",
        cost_usd=0.0,
        latency_ms=1,
        iterations=1,
    )
    records = await recorder.records_for_trace(trace)
    turn = records[-1]
    assert turn["truncated"] is True
    # обрезанный по байтам JSON не парсится — и это единственная честная реакция: «содержимое
    # усечено, ход воспроизводится не целиком», а не «дособчиняем недостающее»
    assert await recorder.record_input(turn) is None
    assert len(turn["input"] or "") <= 4096 * 2


@pytest.mark.usefixtures("db")
async def test_missing_blob_is_a_distinct_diagnosis() -> None:
    """Целостность цепочки и наличие содержимого — два разных диагноза, и их надо различать.

    Содержимое контент-адресовано, поэтому для теста берём уникальный текст: иначе удалённый блоб
    откусил бы вход чужим записям (и соседним запускам) — «сломай всё вокруг» вместо теста.
    """
    recorder = SqlDecisionRecorder(_cfg())
    trace = str(uuid.uuid4())
    await recorder.turn_summary(
        trace_id=trace,
        turn_no=1,
        owner_id=779,
        messages=[{"role": "user", "content": f"уникальный маркер {trace}"}],
        answer=f"ответ {trace}",
        model="glm-4.7",
        cost_usd=0.0,
        latency_ms=1,
        iterations=1,
    )
    async with session() as s:
        sha = await s.scalar(
            text(
                "SELECT input_sha FROM governance.decision_records "
                "WHERE trace_id = CAST(:t AS uuid) AND kind = 'turn_summary'"
            ),
            {"t": trace},
        )
        assert sha is not None
        await s.execute(text("DELETE FROM platform.blobs WHERE sha256 = :sha"), {"sha": sha})
        await s.commit()
    report = await recorder.verify(limit=200_000)
    assert report.ok, "утеря содержимого не имеет права выглядеть как подмена истории"
    assert report.missing_blobs >= 1
    assert "утраченное содержимое" in report.summary()
    again = await recorder.records_for_trace(trace)
    assert again[-1]["input"] is None, "рекордер обязан показать отсутствие входа, а не упасть"


@pytest.mark.usefixtures("db")
async def test_anchor_of_the_day_links_records_and_can_be_recomputed() -> None:
    recorder = SqlDecisionRecorder(_cfg())
    await _one_turn(recorder)
    first = await recorder.anchor()
    assert first.ok, first.note
    assert first.records >= 1 and len(first.merkle_root) == 64
    again = await recorder.anchor()
    assert again.merkle_root == first.merkle_root, "тот же день, те же записи → тот же корень"
    async with session() as s:
        row = (
            (
                await s.execute(
                    text(
                        "SELECT day, records, verified_at FROM governance.anchors WHERE day = :day"
                    ),
                    {"day": date.fromisoformat(first.day)},
                )
            )
            .mappings()
            .one()
        )
    assert str(row["day"]) == first.day and row["records"] == first.records


@pytest.mark.usefixtures("db")
async def test_traces_are_found_by_prefix_and_last_turn_is_addressable() -> None:
    recorder = SqlDecisionRecorder(_cfg())
    trace = await _one_turn(recorder)
    assert await recorder.matching_traces(trace[:8]) == [trace]
    assert await recorder.matching_traces("0" * 7) == []
    assert await recorder.matching_traces("аи") == []  # не UUID и не кириллица — сразу отказ
    assert await recorder.latest_trace(owner_id=777) is not None
    assert await recorder.latest_trace(owner_id=999_999) is None
