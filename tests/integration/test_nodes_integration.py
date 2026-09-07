"""Узлы на живом Postgres: связка, очередь с арендой строки, одиночный победитель settle.

Мок не видит главного: `FOR UPDATE SKIP LOCKED`, ON CONFLICT перевыпуска, CHECK статусов и то,
что «ответ потерян» и «просрочено» — разные финалы с разными словами владельцу. Здесь ровно они.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text

from aegis.planning.nodes import SqlNodeStore
from aegis.platform.config import override_settings
from aegis.platform.db import reset_engine, session

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
                await s.execute(text("SELECT 1 FROM planning.nodes LIMIT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"миграция 0012 не накатана ({type(exc).__name__}) — `make migrate`")
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
                text("DELETE FROM planning.node_commands WHERE owner_id = :o"), {"o": value}
            )
            await s.execute(text("DELETE FROM planning.nodes WHERE owner_id = :o"), {"o": value})
            await s.commit()


@pytest_asyncio.fixture
def store() -> SqlNodeStore:
    return SqlNodeStore()


async def _node_state(node_id: str) -> dict:
    async with session() as s:
        row = (
            (
                await s.execute(
                    text(
                        "SELECT status, pairing_hash IS NULL AS hash_clear FROM planning.nodes"
                        " WHERE id = CAST(:i AS uuid)"
                    ),
                    {"i": node_id},
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


async def _cmd_state(cmd_id: str) -> dict:
    async with session() as s:
        row = (
            (
                await s.execute(
                    text(
                        "SELECT status, result, error FROM planning.node_commands"
                        " WHERE id = CAST(:i AS uuid)"
                    ),
                    {"i": cmd_id},
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


# ---------- связка ----------


async def test_pair_flow_wrong_code_revokes_and_reenroll_recovers(
    store: SqlNodeStore, owner: int
) -> None:
    name = f"pc-{uuid.uuid4().hex[:8]}"
    node_id, code = await store.enroll(owner_id=owner, name=name)
    assert not await store.pair(name=name, code="000001" if code != "000001" else "000002")
    # кривой код закрывает слот (антибрутфорс) — узел перевыпускается владельцем заново
    assert (await _node_state(node_id))["status"] == "revoked"
    node_id2, code2 = await store.enroll(owner_id=owner, name=name)
    assert node_id2 == node_id  # ON CONFLICT: та же строка, новый код
    assert code2 != code
    assert await store.pair(name=name, code=code2)
    state = await _node_state(node_id)
    assert state["status"] == "paired" and state["hash_clear"]  # код больше не хранится


async def test_pair_expired_code_revokes(store: SqlNodeStore, owner: int) -> None:
    name = f"pc-{uuid.uuid4().hex[:8]}"
    node_id, code = await store.enroll(owner_id=owner, name=name)
    async with session() as s:
        await s.execute(
            text(
                "UPDATE planning.nodes SET pair_expires_at = now() - interval '1 minute'"
                " WHERE id = CAST(:i AS uuid)"
            ),
            {"i": node_id},
        )
        await s.commit()
    assert not await store.pair(name=name, code=code)  # верный код, но срок вышел
    assert (await _node_state(node_id))["status"] == "revoked"


async def test_pair_unknown_name_and_revoke(store: SqlNodeStore, owner: int) -> None:
    assert not await store.pair(name="нет-такого", code="123456")
    name = f"pc-{uuid.uuid4().hex[:8]}"
    node_id, code = await store.enroll(owner_id=owner, name=name)
    assert await store.pair(name=name, code=code)
    assert await store.revoke(owner_id=owner, name=name)
    assert (await _node_state(node_id))["status"] == "revoked"
    assert not await store.revoke(owner_id=owner, name=name)  # уже отозван — ничего не меняем
    # чужой владелец не отзывает и не видит
    name2 = f"pc-{uuid.uuid4().hex[:8]}"
    node_id2, code2 = await store.enroll(owner_id=owner, name=name2)
    other = owner + 1
    assert await store.revoke(owner_id=other, name=name2) is False
    assert await store.find(owner_id=other, ref=node_id2[:8]) is None
    assert await store.list_nodes(owner_id=other) == []
    assert await store.pair(name=name2, code=code2)  # pair вне владельца — по имени: намеренно


# ---------- очередь и диспетчер ----------


async def test_enqueue_claim_settle_single_winner(store: SqlNodeStore, owner: int) -> None:
    name = f"q-{uuid.uuid4().hex[:8]}"
    node_id, code = await store.enroll(owner_id=owner, name=name)
    assert await store.pair(name=name, code=code)
    node = await store.find(owner_id=owner, ref=name)
    assert node is not None
    cmd_id = await store.enqueue(node=node, owner_id=owner, action="run", payload={"command": "ls"})
    claimed = await store.claim_for_dispatch()
    mine = [(c, n, d) for c, n, d in claimed if c.id == cmd_id]
    assert len(mine) == 1 and mine[0][2] == "send" and mine[0][1] == name
    assert (await _cmd_state(cmd_id))["status"] == "dispatched"
    # повторный тик: уже dispatched, ответ свежий — ждём, вторично не отправляем
    again = [(c, d) for c, _, d in await store.claim_for_dispatch() if c.id == cmd_id]
    assert again == [] or again[0][1] in ("wait", "lost")  # не 'send'
    assert await store.settle(command_id=cmd_id, ok=True, result="файлы", error=None) == owner
    st = await _cmd_state(cmd_id)
    assert st["status"] == "done" and st["result"] == "файлы"
    # запоздалый дубль результата — молча игнор, в чат не пишем дважды
    assert await store.settle(command_id=cmd_id, ok=True, result="дубль", error=None) is None
    assert (await store.list_recent(owner_id=owner))[0]["result_preview"] == "файлы"


async def test_settle_failure_keeps_error(store: SqlNodeStore, owner: int) -> None:
    name = f"q-{uuid.uuid4().hex[:8]}"
    _, code = await store.enroll(owner_id=owner, name=name)
    assert await store.pair(name=name, code=code)
    node = await store.find(owner_id=owner, ref=name)
    assert node is not None
    cmd_id = await store.enqueue(node=node, owner_id=owner, action="system", payload={})
    await store.claim_for_dispatch()
    assert await store.settle(command_id=cmd_id, ok=False, result=None, error="нет scrot") == owner
    st = await _cmd_state(cmd_id)
    assert st["status"] == "failed" and st["error"] == "нет scrot"


async def test_offline_parks_and_expiry_closes(store: SqlNodeStore, owner: int) -> None:
    name = f"q-{uuid.uuid4().hex[:8]}"
    node_id, code = await store.enroll(owner_id=owner, name=name)
    assert await store.pair(name=name, code=code)
    async with session() as s:  # «ноутбук уснул»: last_seen протухает
        await s.execute(
            text(
                "UPDATE planning.nodes SET last_seen = now() - interval '10 minutes'"
                " WHERE id = CAST(:i AS uuid)"
            ),
            {"i": node_id},
        )
        await s.commit()
    node = await store.find(owner_id=owner, ref=name)
    assert node is not None
    cmd_id = await store.enqueue(
        node=node, owner_id=owner, action="notify", payload={"text": "пак"}, ttl_seconds=600
    )
    decisions = {c.id: d for c, _, d in await store.claim_for_dispatch()}
    assert decisions.get(cmd_id) == "parked"
    assert (await _cmd_state(cmd_id))["status"] == "queued"  # припаркована, не убита
    async with session() as s:
        await s.execute(
            text(
                "UPDATE planning.node_commands SET expires_at = now() - interval '1 second'"
                " WHERE id = CAST(:i AS uuid)"
            ),
            {"i": cmd_id},
        )
        await s.commit()
    decisions = {c.id: d for c, _, d in await store.claim_for_dispatch()}
    assert decisions.get(cmd_id) == "expired"
    st = await _cmd_state(cmd_id)
    assert st["status"] == "expired"
    assert (
        await store.settle(command_id=cmd_id, ok=True, result="слишком поздно", error=None) is None
    )


async def test_lost_answer_fails_with_honest_wording(store: SqlNodeStore, owner: int) -> None:
    name = f"q-{uuid.uuid4().hex[:8]}"
    node_id, code = await store.enroll(owner_id=owner, name=name)
    assert await store.pair(name=name, code=code)
    node = await store.find(owner_id=owner, ref=name)
    assert node is not None
    cmd_id = await store.enqueue(node=node, owner_id=owner, action="run", payload={"command": "ls"})
    await store.claim_for_dispatch()  # → dispatched
    async with session() as s:  # ответ не пришёл: отматываем dispatch назад
        await s.execute(
            text(
                "UPDATE planning.node_commands SET dispatched_at = now() - interval '6 minutes'"
                " WHERE id = CAST(:i AS uuid)"
            ),
            {"i": cmd_id},
        )
        await s.commit()
    decisions = {c.id: d for c, _, d in await store.claim_for_dispatch()}
    assert decisions.get(cmd_id) == "lost"
    st = await _cmd_state(cmd_id)
    assert st["status"] == "failed"
    assert "могла исполниться" in str(st["error"])


async def test_touch_only_on_paired_and_claim_skips_other_owners(
    store: SqlNodeStore, owner: int
) -> None:
    name = f"t-{uuid.uuid4().hex[:8]}"
    node_id, code = await store.enroll(owner_id=owner, name=name)
    await store.touch(node_id=node_id, caps={"os": "linux"})  # pending: молча мимо
    async with session() as s:
        seen = (
            await s.execute(
                text("SELECT last_seen FROM planning.nodes WHERE id = CAST(:i AS uuid)"),
                {"i": node_id},
            )
        ).scalar()
    assert seen is None
    assert await store.pair(name=name, code=code)
    await store.touch(node_id=node_id, caps={"os": "linux", "hostname": "x"})
    node = await store.find(owner_id=owner, ref=name)
    assert node is not None and node.caps.get("os") == "linux"
    paired = await store.paired_nodes_online()
    assert any(n.id == node_id for n in paired)


async def test_cancel_only_queued(store: SqlNodeStore, owner: int) -> None:
    name = f"c-{uuid.uuid4().hex[:8]}"
    node_id, code = await store.enroll(owner_id=owner, name=name)
    assert await store.pair(name=name, code=code)
    node = await store.find(owner_id=owner, ref=name)
    assert node is not None
    cmd_id = await store.enqueue(node=node, owner_id=owner, action="system", payload={})
    assert await store.cancel(owner_id=owner, ref=cmd_id[:8])
    assert (await _cmd_state(cmd_id))["status"] == "cancelled"
    assert not await store.cancel(owner_id=owner, ref=cmd_id[:8])  # уже закрыта
    cmd2 = await store.enqueue(node=node, owner_id=owner, action="system", payload={})
    await store.claim_for_dispatch()
    assert not await store.cancel(owner_id=owner, ref=cmd2[:8])  # улетела — отменить нельзя
    # и истечение по ttl её догонит как 'settled'-защита: status cancelled не меняется
    assert (await _cmd_state(cmd2))["status"] == "dispatched"
    async with session() as s:
        await s.execute(
            text(
                "UPDATE planning.node_commands SET dispatched_at = now() - interval '6 minutes'"
                " WHERE id = CAST(:i AS uuid)"
            ),
            {"i": cmd2},
        )
        await s.commit()
    await store.claim_for_dispatch()
    assert (await _cmd_state(cmd2))["status"] == "failed"  # lost закрывает и её


async def test_enqueue_validates_before_db(store: SqlNodeStore, owner: int) -> None:
    name = f"v-{uuid.uuid4().hex[:8]}"
    node_id, code = await store.enroll(owner_id=owner, name=name)
    assert await store.pair(name=name, code=code)
    node = await store.find(owner_id=owner, ref=name)
    assert node is not None
    with pytest.raises(ValueError, match="пустая команда"):
        await store.enqueue(node=node, owner_id=owner, action="run", payload={"command": " "})
    async with session() as s:
        n = (
            await s.execute(
                text(
                    "SELECT count(*) FROM planning.node_commands WHERE node_id = CAST(:i AS uuid)"
                ),
                {"i": node_id},
            )
        ).scalar()
    assert n == 0  # отказ — не «строка с мусором в очереди»
