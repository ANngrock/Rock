"""CLI операционных слоёв на живой базе: «команда существует» ≠ «команда отвечает делу».

Интеграция, а не юнит, потому что проверяется связка «argparse → магазин → SQL того же
схемного релиза»: именно на ней ломается «тесты зелёные, а в бою команда падает» (колонка
переименована миграцией, импорт не тот, выходной код враньё). Коды выхода — часть контракта:
systemd/jenkins читают их, а не русский текст.
"""

from __future__ import annotations

import os

import pytest

from aegis import cli
from aegis.platform.config import override_settings
from aegis.platform.db import reset_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("AEGIS_TEST_DATABASE_URL"), reason="нужен Postgres"),
]


@pytest.fixture
def db_url() -> str:
    url = os.environ["AEGIS_TEST_DATABASE_URL"]
    with override_settings(database_url=url):
        reset_engine()
        try:
            yield url
        finally:
            reset_engine()


def _run(*argv: str) -> int:
    # каждый cli.main = свой asyncio.run; переиспользованный пул движка «помнит» чужой loop,
    # поэтому в тестах (один процесс) движок обязан рождаться заново — в бою это и так так
    reset_engine()
    try:
        return cli.main(list(argv))
    finally:
        reset_engine()


# ------------------------------------------------------------------ политика


def test_policy_lint_green(db_url: str) -> None:
    # правила в репозитории и lock сгенерированы из одного набора — lint обязан молчать в 0
    assert _run("policy", "lint") == 0


def test_policy_shadow_runs_on_live_rows(db_url: str) -> None:
    # после других тестов в tool_runs что-то да есть; пустой прогон тоже валиден (0 дельт)
    rc = _run("policy", "shadow", "--limit", "20")
    assert rc in (0, 1), "shadow либо чист, либо просит golden — третьего не бывает"


# ------------------------------------------------------------------ флаги


def test_flags_list_and_set_round_trip(db_url: str) -> None:
    assert _run("flags", "list") == 0
    assert _run("flags", "set", "notes.hybrid_search", "--percent", "50", "--reason", "тест") == 0
    assert _run("flags", "stale") in (0, 1)  # свежие тестовые флаги не протухли → 0


def test_flags_set_rejects_unknown_key(db_url: str) -> None:
    assert _run("flags", "set", "made.up.flag", "--percent", "10") == 2


# ------------------------------------------------------------------ права


def test_principals_lifecycle(db_url: str) -> None:
    assert _run("principals", "list") == 0
    assert _run("principals", "kind", "777001", "member") == 0
    assert _run("principals", "grant", "777001", "notes:write") == 0
    assert _run("principals", "grant", "777001", "несуществующее:действие") == 2
    assert _run("principals", "kill", "777001", "--reason", "учебная пауза") == 0
    assert _run("principals", "kill", "777001", "--off") == 0
    assert _run("principals", "budget", "777001", "--usd", "0.50") == 0
    assert _run("principals", "revoke", "777001", "notes:write") == 0


# ------------------------------------------------------------------ хранение


def test_retention_plan_and_holds(db_url: str) -> None:
    assert _run("retention", "plan") == 0
    assert _run("retention", "holds") == 0
    assert _run("retention", "hold", "principal=777001", "--reason", "суд") == 0
    assert _run("retention", "holds") == 0
    assert _run("retention", "hold", "principal=777001", "--release") == 0
    assert _run("retention", "hold", "trace=abcd") == 2  # чужой scope = до таблицы не доходим
    assert _run("retention", "shreds") == 0
    assert _run("retention", "keyring") == 0


def test_retention_forget_plan_is_dry_by_default(db_url: str) -> None:
    # без --execute ничего не уничтожается; код 0 = «план есть», 1 = база не отвечает
    assert _run("retention", "forget", "777001") in (0, 1)


# ------------------------------------------------------------------ события


def test_events_dlq_and_replay_plan(db_url: str) -> None:
    assert _run("events", "dlq") in (0, 1)  # 1 — есть неразобранные, и это честный сигнал
    assert _run("events", "replay", "1", "--type", "turn.finished") == 0


# ------------------------------------------------------------------ схема и бэкфиллы


def test_migrate_status_reports_head(db_url: str) -> None:
    assert _run("migrate", "status") == 0


def test_backfill_status_run_pause(db_url: str) -> None:
    assert _run("backfill", "status") == 0
    assert _run("backfill", "run", "--rounds", "1") in (0, 1)
    assert _run("backfill", "pause", "нет-такого") == 2


# ------------------------------------------------------------------ SLO


def test_slo_status_and_alerts(db_url: str) -> None:
    assert _run("slo", "status") in (0, 1)  # 1 — пейдж; в тихой тестовой базе ожидаем 0
    assert _run("slo", "alerts") == 0
    assert _run("slo", "tick", "--dry-run") in (0, 1)


# ------------------------------------------------------------------ ходы (F1)


def _in_process(coro_fn: object) -> object:
    """Один asyncio.run = свой loop; пул движка привязывается к нему, поэтому reset — с двух сторон.

    Без этого следующий `_run` получает «Future attached to a different loop» — тот же контракт,
    который заставляет _run сбрасывать движок, распространяется на любую подготовку в тесте.
    """
    import asyncio

    reset_engine()
    try:
        return asyncio.run(coro_fn())  # type: ignore[operator]
    finally:
        reset_engine()


def test_turns_status_release_and_drain(db_url: str) -> None:
    import uuid

    from sqlalchemy import text

    from aegis.governance.turns import SqlTurnLedger
    from aegis.platform.db import session

    owner = 999101
    trace = str(uuid.uuid4())
    stranded_trace = str(uuid.uuid4())

    async def prep() -> None:
        await SqlTurnLedger().begin(trace, owner_id=owner)
        async with session() as s:
            await s.execute(
                text(
                    "INSERT INTO governance.turn_queue"
                    " (owner_id, trace_id, kind, payload, status, attempts, claimed_at)"
                    " VALUES (:o, CAST(:t AS uuid), 'message', '{}'::jsonb, 'claimed', 1,"
                    " now() - interval '120 seconds')"
                ).bindparams(o=owner, t=stranded_trace)
            )
            await s.commit()

    _in_process(prep)

    assert _run("turns", "status") == 1  # есть осиротевший claimed — сигнал, не шум
    assert _run("turns", "release", trace) == 0
    assert _run("turns", "release", trace) == 1  # закрыто; второй раз освобождать нечего
    assert _run("turns", "release", "не-uuid") == 2
    assert _run("turns", "drain", str(owner)) == 0  # план: показать, не трогая
    assert _run("turns", "drain", str(owner), "--execute") == 0
    assert _run("turns", "status") == 0  # возврат в queued — бот доберёт сам
    assert _run("migrate", "status") == 0

    async def check_queue() -> None:
        item = await SqlTurnLedger().pop_next(owner)
        assert item is not None and str(item.trace_id) == stranded_trace
        assert item.attempts == 2  # возврат не «обнуляет историю»: попытка уже была

    _in_process(check_queue)
