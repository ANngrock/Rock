"""Хаб автоматизации на живом Postgres: печать секретов, COALESCE, SKIP LOCKED, once-ряды.

Именно тот слой, где мок врёт: sealed-колонка обязана лежать в ``aeg1s:``, «изменить
эндпоинт без секретов» обязано сохранить прежние секреты (COALESCE), аренда вызревших
рядов — двигать next_run тем же коммитом, а ``once`` — гасить статус без второго прохода.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from aegis.automation.store import SqlAutomationStore
from aegis.planning.jobs import SqlJobStore
from aegis.planning.tasks import SqlTaskStore
from aegis.platform.config import override_settings
from aegis.platform.db import reset_engine, session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("AEGIS_TEST_DATABASE_URL"), reason="нужен Postgres"),
]

KEK = base64.b64encode(bytes(range(1, 33))).decode()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[None]:
    url = os.environ["AEGIS_TEST_DATABASE_URL"]
    with override_settings(database_url=url):
        reset_engine()
        try:
            async with session() as s:
                ok = (
                    (await s.execute(text("SELECT to_regclass('automation.endpoint') AS r")))
                    .mappings()
                    .one()["r"]
                )
            if not ok:
                pytest.skip("миграция 0016 не накатана")
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"миграция 0016 не накатана ({type(exc).__name__})")
        yield
        reset_engine()


def _owner() -> int:
    return int(uuid.uuid4().int % 999_999) + 10_000


async def _raw_col(table: str, col: str, owner: int, key: str) -> str:
    async with session() as s:
        row = (
            (
                await s.execute(
                    text(
                        f"SELECT {col} AS v FROM {table} WHERE owner_id = :o"  # noqa: S608,PLW0603
                    ),
                    {"o": owner},
                )
            )
            .mappings()
            .first()
        )
    return str((row or {}).get("v") or "")


async def test_endpoint_secrets_sealed_and_preserved(db: None) -> None:
    o = _owner()
    with override_settings(crypto_kek=KEK, crypto_mode="auto"):
        store = SqlAutomationStore()
        row = await store.upsert_endpoint(
            owner_id=o,
            name="deploy",
            url="https://example.com/hook",
            headers={"X-Key": "{{secret:TOKEN}}"},
            secrets_map={"TOKEN": "знач-321"},
        )
        assert row.secret_names == ("TOKEN",)
        raw = await _raw_col("automation.endpoint", "secrets", o, "secrets")
        assert raw.startswith("aeg1s:") and "знач-321" not in raw
        # обновление без поля secrets не должно съедать прежние секреты
        again = await store.upsert_endpoint(
            owner_id=o, name="deploy", url="https://example.com/hook2"
        )
        assert again.secret_names == ("TOKEN",)
        got, secrets = await store.endpoint_for_run(owner_id=o, ref="deploy")
        assert secrets == {"TOKEN": "знач-321"} and got.url.endswith("/hook2")
        # чужой владелец не видит и не печатает
        other = await store.list_endpoints(owner_id=o + 1)
        assert other == []
        with pytest.raises(KeyError):
            await store.endpoint_for_run(owner_id=o + 1, ref="deploy")


async def test_endpoint_secrets_refused_without_kek(db: None) -> None:
    o = _owner()
    with override_settings(crypto_mode="off"):
        store = SqlAutomationStore()
        with pytest.raises(ValueError, match="ключарк"):
            await store.upsert_endpoint(
                owner_id=o, name="bare", url="https://example.com/x", secrets_map={"A": "b"}
            )
        # без секретов — можно и в open-режиме
        row = await store.upsert_endpoint(owner_id=o, name="bare", url="https://example.com/x")
        assert row.secret_names == ()


async def test_runs_update_last_status(db: None) -> None:
    o = _owner()
    store = SqlAutomationStore()
    row = await store.upsert_endpoint(owner_id=o, name="ci", url="https://example.com/ci")
    await store.record_run(
        endpoint_id=row.id,
        owner_id=o,
        ok=True,
        status=204,
        ms=33,
        digest="HTTP 204 · ок",
        triggered_by="cli",
    )
    await store.record_run(
        endpoint_id=row.id,
        owner_id=o,
        ok=False,
        status=500,
        ms=120,
        digest="HTTP 500 · boom",
        triggered_by="model",
    )
    runs = await store.recent_runs(owner_id=o, limit=5)
    assert [r["ok"] for r in runs] == [False, True] and runs[0]["name"] == "ci"
    listed = await store.list_endpoints(owner_id=o)
    assert listed[0].last_status == "fail 500"
    # удаление эндпоинта снимает и журнал (CASCADE)
    assert await store.drop_endpoint(owner_id=o, ref="ci")
    assert await store.recent_runs(owner_id=o, limit=5) == []


async def test_hook_unique_name_and_secret_roundtrip(db: None) -> None:
    o1, o2 = _owner(), _owner()
    store = SqlAutomationStore()
    kek = override_settings(crypto_kek=KEK, crypto_mode="auto")
    kek.__enter__()
    hook, secret = await store.add_hook(owner_id=o1, name=f"ci{o1}", policy="turn", rate_per_min=3)
    assert hook.policy == "turn" and len(secret) > 20
    found = await store.hook_by_name(f"ci{o1}")
    assert found is not None
    got_row, got_secret = found
    assert got_secret == secret and got_row.owner_id == o1
    # имя занято — даже второму владельцу
    with pytest.raises(ValueError, match="занято"):
        await store.add_hook(owner_id=o2, name=f"ci{o1}")
    note = await store.set_hook_enabled(owner_id=o1, ref=f"ci{o1}", enabled=False)
    assert note is not None and "выключен" in note
    found = await store.hook_by_name(f"ci{o1}")
    assert found is not None and not found[0].enabled
    await store.bump_fire(hook.id)
    listed = await store.list_hooks(owner_id=o1)
    assert listed[0].fires == 1
    # чужой ref не переключается
    assert await store.set_hook_enabled(owner_id=o2, ref=f"ci{o1}", enabled=True) is None
    kek.__exit__(None, None, None)


async def test_task_lifecycle_and_due_claim(db: None) -> None:
    o = _owner()
    store = SqlTaskStore()
    due = datetime.now(UTC) + timedelta(days=1)
    row = await store.add(
        owner_id=o, title="починить сарай", due_at=due, priority=2, remind_on_due=True
    )
    assert row.id and row.priority == 2
    items = await store.list_tasks(owner_id=o)
    assert [t.title for t in items] == ["починить сарай"]
    # созрел не был — claim пуст; подвигаем срок в прошлое
    ripe = await store.claim_due_reminders(now=datetime.now(UTC), limit=10)
    assert [t for t in ripe if t.id == row.id] == []
    past = await store.update(
        owner_id=o, ref=row.id[:8], due_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    assert past is not None
    claimed = [
        t
        for t in await store.claim_due_reminders(now=datetime.now(UTC), limit=50)
        if t.id == row.id
    ]
    assert len(claimed) == 1
    # второй claim тот же ряд не отдаёт: флажок снят тем же коммитом
    again = [
        t
        for t in await store.claim_due_reminders(now=datetime.now(UTC), limit=50)
        if t.id == row.id
    ]
    assert again == []
    done = await store.update(owner_id=o, ref="сарай", status="done")
    assert done is not None and done.status == "done" and done.done_at is not None
    stat = await store.stats(owner_id=o)
    assert stat["open"] == 0 and stat["done_week"] == 1
    # «сарай» больше не матчится как active
    assert await store.list_tasks(owner_id=o, status="active") == []
    # несколько совпадений по слову — одна строка: update закроет ровно первую по updated_at
    for t in ("общее дело А", "общее дело Б"):
        await store.add(owner_id=o, title=t, priority=1)
    one = await store.update(owner_id=o, ref="общее", status="doing")
    assert one is not None
    doing = [
        t for t in await store.list_tasks(owner_id=o, status="doing") if t.title.startswith("общее")
    ]
    assert len(doing) == 1


async def test_job_claim_advances_and_once_closes(db: None) -> None:
    o = _owner()
    store = SqlJobStore()
    now = datetime.now(UTC)
    job_id = await store.add(
        owner_id=o,
        title="сводка",
        prompt="собери",
        repeat="daily",
        at_time="08:00",
        tz=UTC,
        first_run=now - timedelta(minutes=1),
    )
    due = await store.claim_due(limit=5, now=now, tz=UTC)
    mine = [j for j in due if j.id == job_id]
    assert len(mine) == 1 and mine[0].next_run <= now  # в ряду из claim next_run ещё старый
    # после claim следующий тик этого ряда не отдаст (next_run уехал вперёд)
    due2 = await store.claim_due(limit=5, now=now + timedelta(seconds=5), tz=UTC)
    assert [j for j in due2 if j.id == job_id] == []
    # пять падений — авто-пауза
    fresh = await store.resolve(owner_id=o, ref=job_id[:8])
    assert fresh is not None
    for _ in range(5):
        await store.finish_run(fresh, ok=False, error="boom")
    paused = await store.resolve(owner_id=o, ref=job_id[:8])
    assert paused is not None and paused.status == "paused" and paused.fail_count == 5
    # success-прогон обнуляет счётчик и не будит паузу
    await store.set_status(owner_id=o, ref=job_id[:8], status="active")
    active = await store.resolve(owner_id=o, ref=job_id[:8])
    assert active is not None
    await store.finish_run(active, ok=True, error="")
    ok_row = await store.resolve(owner_id=o, ref=job_id[:8])
    assert ok_row is not None and ok_row.fail_count == 0 and ok_row.status == "active"


async def test_job_once_trigger_and_unique_title(db: None) -> None:
    o = _owner()
    store = SqlJobStore()
    at = datetime.now(UTC) + timedelta(days=1)
    jid = await store.add(
        owner_id=o, title="раз", prompt="сделай X", repeat="once", first_run=at, tz=UTC
    )
    # once: add без first_run запрещён
    with pytest.raises(ValueError, match="once"):
        await store.add(owner_id=o, title="два", prompt="Y", repeat="once", tz=UTC)
    # UNIQUE(owner,title) — повтор это upsert, а не вторая строка
    jid2 = await store.add(
        owner_id=o, title="раз", prompt="сделай Z", repeat="once", first_run=at, tz=UTC
    )
    assert jid2 == jid
    trig = await store.trigger_now(owner_id=o, ref="раз")
    assert trig is not None
    due = await store.claim_due(limit=5, now=datetime.now(UTC) + timedelta(minutes=1), tz=UTC)
    once = [j for j in due if j.id == jid]
    assert len(once) == 1 and once[0].repeat == "once"
    row = await store.resolve(owner_id=o, ref=jid[:8])
    assert row is not None and row.status == "done"  # once закрыт арендой
    # после закрытия больше не созревает
    due2 = await store.claim_due(limit=5, now=datetime.now(UTC) + timedelta(hours=1), tz=UTC)
    assert [j for j in due2 if j.id == jid] == []


async def test_counts_for_doctor(db: None) -> None:
    store = SqlAutomationStore()
    c: dict[str, Any] = await store.counts()
    assert set(c) == {"endpoints", "hooks", "recent_fails"}
    assert c["endpoints"] >= 0 and c["hooks"] >= 0
