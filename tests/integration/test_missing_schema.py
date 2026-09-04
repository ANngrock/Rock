"""Живой Postgres без схемы: бот отвечает, трассировка деградирует (регрессия на «Сбой: TypeError»).

Чистый кластер Postgres без накатанных миграций — ровно то состояние, в котором оказался рабочий
контейнер: `db_ready=true` (порт отвечает), таблиц нет. Раньше в этом положении каждое сообщение
кончалось TypeError (падал сам обработчик отказа), а `/status` врал, что с БД всё в порядке.
Нужен пакет `pgserver` (есть в extra `dev`), иначе скип.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module")
def bare_pg_url() -> Iterator[str]:
    """URL чистого Postgres 16 без схемы aegis."""
    pgserver = pytest.importorskip("pgserver")
    datadir = Path(tempfile_dir())
    server = pgserver.get_server(str(datadir), cleanup_mode="stop")
    info = server.get_postmaster_info()
    assert info is not None and info.socket_dir
    yield f"postgresql+asyncpg://postgres@/postgres?host={info.socket_dir}"


def tempfile_dir() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="aegis-bare-")


async def test_handle_answers_when_schema_is_missing(bare_pg_url: str) -> None:
    from aegis.agents.services import Services
    from aegis.agents.supervisor import Inbound, Supervisor
    from aegis.agents.tools.registry import ToolRegistry
    from aegis.governance.audit import SqlAuditLog
    from aegis.governance.policy import PolicyEngine
    from aegis.platform.config import override_settings
    from aegis.platform.db import reset_engine
    from aegis.platform.events.sink import BestEffortEventSink, OutboxEventSink
    from aegis.platform.gateway.cost import CostGovernor
    from conftest import FakeFacts, FakeGateway, FakeKV, FakeNotes, make_chat_result

    with override_settings(database_url=bare_pg_url, glm_api_key="test-key") as cfg:
        reset_engine()
        try:
            events = BestEffortEventSink(OutboxEventSink())  # как в runtime.build_app
            kv = FakeKV()
            gateway = FakeGateway([make_chat_result("Привет! Я Aegis.")], CostGovernor(kv, 2.0))
            supervisor: Any = Supervisor(
                services=Services(
                    gateway=gateway,  # type: ignore[arg-type]
                    facts=FakeFacts(),  # type: ignore[arg-type]
                    notes=FakeNotes(),  # type: ignore[arg-type]
                ),
                registry=ToolRegistry(),
                policy=PolicyEngine(auto_allow_low_risk=True),
                kv=kv,  # type: ignore[arg-type]
                cfg=cfg,
                events=events,  # type: ignore[arg-type]
                audit=SqlAuditLog(),  # type: ignore[arg-type]
            )
            reply = await supervisor.handle(Inbound(text="расскажи про себя", owner_id=1))
            assert reply.text == "Привет! Я Aegis.", (
                "ответ обязан быть, даже когда трассировка лежит"
            )
            assert events.degraded is True, "о деградации надо знать, а не молчать"
            assert events.failures >= 1
            status = await supervisor.status(1)
            assert status["tracing_degraded"] is True, "трасса лежит — статус обязан это показывать"
            assert status["tracing_failures"] >= 1
        finally:
            reset_engine()


async def test_probe_schema_reports_missing_tables(bare_pg_url: str) -> None:
    from aegis.platform.config import override_settings
    from aegis.platform.db import reset_engine

    with override_settings(database_url=bare_pg_url, glm_api_key="test-key"):
        reset_engine()
        try:
            from aegis.agents.tools.registry import registry as global_registry
            from aegis.runtime import build_app

            app = build_app(registry=global_registry, configure_logging=False)
            try:
                assert app.db_ready is True
                assert await app.probe_schema() is False, "без миграций probe обязан сказать «нет»"
            finally:
                await app.aclose()
        finally:
            reset_engine()
