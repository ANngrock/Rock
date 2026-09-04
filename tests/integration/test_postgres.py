"""Интеграция с реальным Postgres: event store, outbox, append-only, поиск, audit.

Запускается только при заданном ``AEGIS_TEST_DATABASE_URL`` (в CI — сервис pgvector, локально —
``make up-core`` + ``make test-all``). Здесь проверяется именно SQL: то, что мок не проверит.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text

from aegis.knowledge.notes import NotesRepo
from aegis.memory.facts import FactsRepo
from aegis.platform.config import override_settings
from aegis.platform.db import reset_engine, session
from aegis.platform.events.store import ConcurrencyError, EventStore

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
                await s.execute(text("SELECT 1 FROM platform.events LIMIT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"схема не готова ({type(exc).__name__}) — выполните `make migrate`")
        yield
        reset_engine()


# Примечание: «очистки» нет намеренно — events append-only (см. тест выше), поэтому идентификаторы
# потоков в тестах уникальны (uuid), а таблицы намеренно растут: это журнал, а не рабочее состояние.


@pytest.mark.usefixtures("db")
async def test_event_store_append_and_load() -> None:
    stream = f"owner:{uuid.uuid4()}"
    async with session() as s:
        store = EventStore(s)
        first = await store.append(
            stream_type="owner",
            stream_id=stream,
            event_type="conversation.turn_received",
            payload={"text": "привет"},
            metadata={"prompt_version": "sys-v0.2.0"},
        )
        second = await store.append(
            stream_type="owner",
            stream_id=stream,
            event_type="tool.executed",
            payload={"tool": "web_search", "ok": True},
        )
        assert second > first
        events = await store.load(stream)
        assert [e["version"] for e in events] == [1, 2]
        assert events[0]["payload"] == {"text": "привет"}
        assert events[0]["metadata"]["prompt_version"] == "sys-v0.2.0"
        assert await store.version(stream) == 2


@pytest.mark.usefixtures("db")
async def test_optimistic_concurrency_rejects_stale_version() -> None:
    stream = f"owner:{uuid.uuid4()}"
    async with session() as s:
        store = EventStore(s)
        await store.append(stream_type="owner", stream_id=stream, event_type="a", payload={})
        with pytest.raises(ConcurrencyError):
            await store.append(
                stream_type="owner",
                stream_id=stream,
                event_type="b",
                payload={},
                expected_version=0,
            )


@pytest.mark.usefixtures("db")
async def test_events_are_append_only() -> None:
    stream = f"owner:{uuid.uuid4()}"
    async with session() as s:
        event_id = await EventStore(s).append(
            stream_type="owner", stream_id=stream, event_type="a", payload={"x": 1}
        )
    with pytest.raises(Exception, match="append-only"):
        async with session() as s:
            await s.execute(
                text("UPDATE platform.events SET payload = '{}'::jsonb WHERE id = :id").bindparams(
                    id=event_id
                )
            )
    with pytest.raises(Exception, match="append-only"):
        async with session() as s:
            await s.execute(
                text("DELETE FROM platform.events WHERE id = :id").bindparams(id=event_id)
            )


@pytest.mark.usefixtures("db")
async def test_outbox_relay_contract() -> None:
    """Relay выбирает непубликованные, помечает — и больше не отдаёт их."""
    stream = f"owner:{uuid.uuid4()}"
    async with session() as s:
        event_id = await EventStore(s).append(
            stream_type="owner", stream_id=stream, event_type="note.added", payload={"note": "1"}
        )
    async with session() as s:
        store = EventStore(s)
        pending = await store.fetch_unpublished(limit=500)
        mine = [row for row in pending if row["event_id"] == event_id]
        assert mine, "событие обязано попасть в outbox той же транзакцией"
        assert mine[0]["payload"] == {"note": "1"}
        await store.mark_published([row["outbox_id"] for row in mine])
    async with session() as s:
        still = [
            row
            for row in await EventStore(s).fetch_unpublished(limit=500)
            if row["event_id"] == event_id
        ]
        assert not still


@pytest.mark.usefixtures("db")
async def test_facts_upsert_and_invalidate() -> None:
    facts = FactsRepo()
    text_value = f"тестовый факт {uuid.uuid4()}"
    first = await facts.add(text_value, "general", source="test", importance=0.3)
    second = await facts.add(text_value, "general", source="test", importance=0.9)
    assert first == second, "повтор факта — upsert, а не дубль в контексте модели"

    listed = await facts.list(200)
    assert any(f.id == first and f.importance == pytest.approx(0.9) for f in listed)

    assert await facts.invalidate(first) is True
    assert all(f.id != first for f in await facts.list(200))
    assert await facts.invalidate(first) is False, "повторное удаление — no-op"


@pytest.mark.usefixtures("db")
async def test_notes_text_search_works_without_embeddings() -> None:
    """Принцип деградации: поиск обязан находить, даже когда векторов нет вообще."""
    notes = NotesRepo()
    marker = f"кофемашина-{uuid.uuid4().hex[:8]}"
    note = await notes.add(
        "Офис", f"Обсудили {marker} и бюджет на кофе", ["office", "test"], source="test"
    )
    hits = await notes.search(marker)
    assert hits and hits[0].id == note.id
    assert hits[0].method in ("text", "like")


@pytest.mark.usefixtures("db")
async def test_notes_vector_search_when_embedding_present() -> None:
    notes = NotesRepo()
    note = await notes.add(
        "Векторная заметка", "уникальное слово зюйдвенд", ["test"], source="test"
    )
    # уникальные компоненты: иначе вечно живущие строки прошлых прогонов дают равное
    # расстояние (0) и assert «нашёл именно эту заметку» превращается в лотерею
    dims = 2048
    seed = uuid.uuid4().int
    vector = [((seed >> (i % 61)) % 1000) / 1000.0 + 0.001 for i in range(dims)]
    await notes.set_embedding(note.id, vector)
    hits = await notes.search("зюйдвенд", vector, 5)
    assert hits, "поиск по эмбеддингу должен найти только что проиндексированную заметку"
    assert hits[0].method == "vector"
    assert hits[0].id == note.id


@pytest.mark.usefixtures("db")
async def test_audit_tables_and_cost_view() -> None:
    from aegis.governance.audit import SqlAuditLog
    from aegis.platform.gateway.client import LLMCallRecord

    trace = str(uuid.uuid4())
    audit = SqlAuditLog()
    await audit.llm_call(
        LLMCallRecord(
            call_id=str(uuid.uuid4()),
            role="brain",
            model="glm-4.6",
            trace_id=trace,
            prompt_tokens=120,
            completion_tokens=30,
            cost_usd=0.0004,
            latency_ms=900,
            ok=True,
        )
    )
    await audit.tool_run(
        trace_id=trace,
        tool="web_search",
        args={"query": "погода"},
        decision="allow",
        result="ясно",
        ok=True,
        owner_id=1,
    )
    async with session() as s:
        agg = (
            await s.execute(
                text(
                    "SELECT count(*) AS calls, sum(cost_usd) AS total FROM platform.v_cost_daily "
                    "WHERE model = 'glm-4.6'"
                )
            )
        ).one()
        assert agg.calls >= 1 and float(agg.total) > 0

        stmt = text(
            "SELECT tool, decision, args->>'query' AS query "
            "FROM governance.tool_runs WHERE trace_id = CAST(:t AS uuid)"
        ).bindparams(t=trace)
        tool_run = (await s.execute(stmt)).one()
        assert tool_run.tool == "web_search"
        assert tool_run.decision == "allow"
        assert tool_run.query == "погода"


@pytest.mark.usefixtures("db")
async def test_supervisor_turn_writes_events_and_audit() -> None:
    """Один ход владельца = полный след в БД (событие + tool_run), через реальные SQL-санки.

    Это ровно тот путь, который не был проверен никогда: юниты гоняются на фейках, а
    `handle()` + SqlAuditLog/OutboxEventSink на настоящном драйвере — только здесь. Именно на
    этом стыке родился «Сбой: TypeError» у владельца (упавший обработчик отказа в sink).
    """
    from pydantic import BaseModel

    from aegis.agents.services import Services
    from aegis.agents.supervisor import Inbound, Supervisor
    from aegis.agents.tools.registry import ToolContext, ToolRegistry
    from aegis.governance.audit import SqlAuditLog
    from aegis.governance.policy import PolicyEngine, Risk
    from aegis.platform.config import Settings
    from aegis.platform.events.sink import BestEffortEventSink, OutboxEventSink
    from aegis.platform.gateway.cost import CostGovernor
    from conftest import FakeFacts, FakeGateway, FakeKV, FakeNotes, make_chat_result

    class Args(BaseModel):
        value: str = ""

    registry = ToolRegistry()

    async def note(args: Args, ctx: ToolContext) -> str:
        return f"записано: {args.value}"

    registry.register("test_note", "тест", Args, writes=False, risk=Risk.NONE)(note)

    kv = FakeKV()
    gateway = FakeGateway(
        [
            make_chat_result(None, [("c1", "test_note", {"value": "раз"})]),
            make_chat_result("готово"),
        ],
        CostGovernor(kv, 2.0),
    )
    events = BestEffortEventSink(OutboxEventSink())
    audit = SqlAuditLog()
    supervisor = Supervisor(
        services=Services(gateway=gateway, facts=FakeFacts(), notes=FakeNotes()),
        registry=registry,
        policy=PolicyEngine(auto_allow_low_risk=True),
        kv=kv,
        cfg=Settings(_env_file=None, glm_api_key="k"),
        events=events,
        audit=audit,
    )
    owner = uuid.uuid4().int % 10**9
    reply = await supervisor.handle(Inbound(text="запиши заметку", owner_id=owner))

    assert "готово" in reply.text
    assert events.degraded is False and events.failures == 0, (
        "на живой схеме деградации быть не должно"
    )
    assert audit.failures == 0

    stream = f"owner:{owner}"
    async with session() as s:
        recorded = await s.scalar(
            text("SELECT count(*) FROM platform.events WHERE stream_id = :stream").bindparams(
                stream=stream
            )
        )
        assert recorded >= 1, "ход обязан оставить событие в append-only журнале"
        tool_run = (
            await s.execute(
                text(
                    "SELECT tool, decision, args->>'value' AS value FROM governance.tool_runs "
                    "WHERE trace_id = CAST(:t AS uuid)"
                ).bindparams(t=reply.trace_id)
            )
        ).one()
        assert (tool_run.tool, tool_run.decision, tool_run.value) == (
            "test_note",
            "allow",
            "раз",
        )
        published = await s.scalar(
            text(
                "SELECT count(*) FROM platform.outbox o "
                "JOIN platform.events e ON e.id = o.event_id "
                "WHERE e.stream_id = :stream"
            ).bindparams(stream=stream)
        )
        assert published >= 1, "событие обязано уйти в outbox той же транзакцией"

    status = await supervisor.status(owner)
    assert status["tracing_degraded"] is False
