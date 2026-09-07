"""Реестр подключений на живом PG: upsert по имени, секрет только шифром, рубильник.

Главный инвариант здесь — «в дампе нет ключей»: проверяем байты колонки, а не только то, что
код store аккуратен. Аккуратность ломается эволюцией (сохранили конфиг вместе с ключом), а
POSITION(plain IN raw) в тесте это ловит.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text

from aegis.integrations.store import SqlConnectorStore
from aegis.platform.config import override_settings
from aegis.platform.db import reset_engine, session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("AEGIS_TEST_DATABASE_URL"), reason="нужен Postgres"),
]

_KEK = base64.b64encode(b"k" * 32).decode()  # тестовый KEK: 32 байта в base64


@pytest_asyncio.fixture
async def db() -> AsyncIterator[None]:
    url = os.environ["AEGIS_TEST_DATABASE_URL"]
    with override_settings(database_url=url, crypto_kek=_KEK):
        reset_engine()
        try:
            async with session() as s:
                await s.execute(text("SELECT 1 FROM integrations.connectors LIMIT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"миграция 0011 не накатана ({type(exc).__name__}) — `make migrate`")
        yield
        reset_engine()


@pytest_asyncio.fixture
async def owner(db: None) -> AsyncIterator[int]:
    value = int(uuid.uuid4().int % 2_000_000_000) + 10_000
    try:
        yield value
    finally:
        async with session() as s:
            await s.execute(
                text("DELETE FROM integrations.connectors WHERE owner_id = :o"), {"o": value}
            )
            await s.commit()


@pytest.mark.usefixtures("db")
async def test_upsert_gating_and_round_trip(owner: int) -> None:
    store = SqlConnectorStore()
    first = await store.add(
        owner_id=owner,
        kind="mcp",
        name="stub",
        config={"command": "python", "args": ["srv.py"], "env": {}, "secret_env": "TOKEN"},
    )
    second = await store.add(
        owner_id=owner, kind="mcp", name="stub", config={"command": "python", "args": []}
    )
    assert first == second, "имя — ключ перезаписи, а не дубль"

    rows = await store.list(owner_id=owner)
    assert len(rows) == 1 and rows[0].config["args"] == []

    # одна строка на kind: mcp+api с одним именем — разные подключения, не конфликт
    await store.add(owner_id=owner, kind="api", name="stub", config={})
    assert len(await store.list(owner_id=owner)) == 2

    assert await store.set_enabled(owner_id=owner, name="stub", enabled=False)
    assert await store.list(owner_id=owner, enabled_only=True) == []
    assert await store.set_enabled(owner_id=owner, name="stub", enabled=True)
    assert len(await store.list(owner_id=owner, enabled_only=True)) == 2  # оба kind

    # remove — по имени во всех kind (имя — ручка владельца, а не (kind,name)): удалил «stub» —
    # значит убрал всё, что называется stub; полу-удалений, оставляющих «вторую половину», нет
    assert await store.remove(owner_id=owner, name="stub")
    assert await store.list(owner_id=owner) == []


@pytest.mark.usefixtures("db")
async def test_secret_encrypted_at_rest_and_restored(owner: int) -> None:
    secret = "sk-live-" + uuid.uuid4().hex
    store = SqlConnectorStore()
    await store.add(
        owner_id=owner, kind="mcp", name="vaulted", config={"command": "x", "secret_env": "API_KEY"}
    )
    await store.set_secret(owner_id=owner, kind="mcp", name="vaulted", plaintext=secret)

    async with session() as s:
        row = (
            (
                await s.execute(
                    text(
                        "SELECT secret_ct, secret_wrapped, secret_key_version,"
                        " POSITION(CAST(:p AS bytea) IN secret_ct) = 0 AS clean"
                        " FROM integrations.connectors"
                        " WHERE owner_id = :o AND kind = 'mcp' AND name = 'vaulted'"
                    ),
                    {"o": owner, "p": secret.encode()},
                )
            )
            .mappings()
            .one()
        )
    assert row["secret_ct"] and row["secret_key_version"] == 1
    assert row["clean"], "шифртекст колонки не содержит ключ ни в каком смещении"

    conns = await store.list(owner_id=owner, enabled_only=True)
    conn = next(c for c in conns if c.name == "vaulted")
    assert await store.secret_for_mcp(owner_id=owner, connector=conn) == secret


@pytest.mark.usefixtures("db")
async def test_probe_records_the_truth(owner: int) -> None:
    store = SqlConnectorStore()
    conn_id = await store.add(owner_id=owner, kind="api", name="metered", config={})
    await store.record_probe(connector_id=conn_id, ok=False, error="HTTP 401")
    rows = await store.list(owner_id=owner)
    conn = rows[0]
    assert conn.last_error == "HTTP 401" and conn.last_ok_at is None
    await store.record_probe(connector_id=conn_id, ok=True)
    conn = (await store.list(owner_id=owner))[0]
    assert conn.last_error is None and conn.last_ok_at is not None


@pytest.mark.usefixtures("db")
async def test_unique_constraint_per_kind_name(owner: int) -> None:
    from sqlalchemy.exc import IntegrityError

    store = SqlConnectorStore()
    await store.add(owner_id=owner, kind="api", name="solo", config={})
    other = int(uuid.uuid4().int % 2_000_000_000) + 10_000
    try:
        with pytest.raises(IntegrityError):
            # обход upsert-пути прямым INSERT тем же именем того же kind — нарушение UNIQUE
            async with session() as s:
                await s.execute(
                    text(
                        "INSERT INTO integrations.connectors (owner_id, kind, name)"
                        " VALUES (:o, 'api', 'solo')"
                    ),
                    {"o": owner},
                )
                await s.commit()
    finally:
        async with session() as s:
            await s.execute(
                text("DELETE FROM integrations.connectors WHERE owner_id = :o"), {"o": other}
            )
            await s.commit()
