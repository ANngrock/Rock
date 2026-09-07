"""Парсер на живом Postgres: UNIQUE-дедуп, SKIP LOCKED, печать тела, статусы.

Мок здесь не подменишь ничем: «ON CONFLICT (source_id, fingerprint)» и аренда
due_sources — именно тот слой, где «в юнитах зелено, в проде дубли». Тело проверяется
уже в конверте: колонка обязана лежать запечатанной, а читаться — открытой.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text

from aegis.parsing.dedup import fingerprint
from aegis.parsing.store import SourceRow, SqlParsingStore
from aegis.platform.config import override_settings
from aegis.platform.crypto import BlobCipher
from aegis.platform.db import reset_engine, session
from aegis.platform.vault import derive_kek

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
                await s.execute(text("SELECT 1 FROM parsing.sources LIMIT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"миграция 0015 не накатана ({type(exc).__name__})")
        yield
        reset_engine()


def _draft(url: str, title: str, body: str) -> dict[str, object]:
    from dataclasses import asdict

    from aegis.parsing.engine import ItemDraft

    return asdict(
        ItemDraft(
            fingerprint=fingerprint(url=url, title=title, text=body),
            title=title,
            url=url,
            text=body,
            excerpt=body[:60],
            author="тест",
            media=["https://cdn1.telesco.pe/a.jpg"],
            published_at=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
        )
    )


@pytest_asyncio.fixture
async def owner() -> int:
    return int(uuid.uuid4().fields[-1] % 10_000_000) + 900_000_000  # уникальный под прогоны


@pytest.mark.usefixtures("db")
class TestSources:
    async def test_add_list_toggle_drop(self, owner: int) -> None:
        store = SqlParsingStore()
        target = f"https://example.org/feed-{uuid.uuid4().hex[:8]}.xml"
        sid = await store.add_source(
            owner_id=owner,
            target=target,
            kind="rss",
            label="тест",
            interval_sec=600,
            include_kw="важно",
            exclude_kw="спам",
        )
        rows = await store.list_sources(owner_id=owner, limit=10)
        src = next(r for r in rows if r.id == sid)
        assert (src.kind, src.label, src.interval_sec) == ("rss", "тест", 600)
        assert src.include_kw == "важно" and src.exclude_kw == "спам"
        assert src.enabled

        again = await store.set_enabled(owner_id=owner, ref=sid[:8], enabled=False)
        assert again is not None
        rows = await store.list_sources(owner_id=owner, limit=10)
        assert not next(r for r in rows if r.id == sid).enabled
        assert await store.set_enabled(owner_id=owner, ref="decafbad", enabled=True) is None
        assert await store.drop(owner_id=owner, ref=sid[:8])
        assert not [r for r in await store.list_sources(owner_id=owner, limit=10) if r.id == sid]

    async def test_readd_is_upsert_not_conflict(self, owner: int) -> None:
        store = SqlParsingStore()
        target = f"https://example.org/feed-{uuid.uuid4().hex[:8]}.xml"
        sid = await store.add_source(
            owner_id=owner, target=target, kind="rss", label="", interval_sec=600
        )
        await store.set_enabled(owner_id=owner, ref=sid[:8], enabled=False)
        again = await store.add_source(
            owner_id=owner, target=target, kind="rss", label="", interval_sec=600
        )
        assert again == sid  # повтор — это «вернись и оживи», не новая строка
        rows = await store.list_sources(owner_id=owner, limit=50)
        assert sum(1 for r in rows if r.target == target) == 1
        assert next(r for r in rows if r.id == sid).enabled  # реанимирован
        with pytest.raises(ValueError):
            await store.add_source(
                owner_id=owner, target="ftp://nope", kind="rss", label="", interval_sec=600
            )

    async def test_due_claims_once_and_cooldown(self, owner: int) -> None:
        store = SqlParsingStore()
        target = f"https://example.org/feed-{uuid.uuid4().hex[:8]}.xml"
        sid = await store.add_source(
            owner_id=owner, target=target, kind="rss", label="", interval_sec=3600
        )
        due = [s.id for s in await store.due_sources(limit=50)]
        assert sid in due
        await store.touch_run(sid)
        assert sid not in [s.id for s in await store.due_sources(limit=50)]
        # отмотали last_check — снова созрел: интервал живой, а не «навсегда»/«никогда»
        async with session() as s:
            await s.execute(
                text(
                    "UPDATE parsing.sources SET last_check = now() - interval '2 hours'"
                    " WHERE id = :i"
                ),
                {"i": uuid.UUID(sid)},
            )
            await s.commit()
        assert sid in [s.id for s in await store.due_sources(limit=50)]


@pytest.mark.usefixtures("db")
class TestItemsAndSeal:
    async def test_dedup_and_seal_roundtrip(self, owner: int) -> None:
        store = SqlParsingStore()
        target = f"https://example.org/feed-{uuid.uuid4().hex[:8]}.xml"
        sid = await store.add_source(
            owner_id=owner, target=target, kind="rss", label="", interval_sec=600
        )
        async with session() as s:
            row = (
                (
                    await s.execute(
                        text("SELECT * FROM parsing.sources WHERE id = :i"), {"i": uuid.UUID(sid)}
                    )
                )
                .mappings()
                .one()
            )
        src = SourceRow(
            id=sid,
            owner_id=owner,
            kind=row["kind"],
            target=row["target"],
            label=row["label"],
            interval_sec=row["interval_sec"],
        )

        cipher = BlobCipher({1: derive_kek(bytes(range(1, 33)), 1)}, active_version=1)
        d1 = _draft("https://e/1?utm_source=x", "Заголовок", "тело про важное")
        n = await store.add_items(source=src, items=[d1], cipher=cipher)
        assert n == 1
        n2 = await store.add_items(  # та же ссылка без utm → тот же fingerprint → ноль
            source=src, items=[_draft("https://e/1", "Заголовок", "тело про важное")], cipher=cipher
        )
        assert n2 == 0

        async with session() as s:  # в колонке — только конверт
            raw = (
                (
                    await s.execute(
                        text("SELECT body, body_sealed FROM parsing.items WHERE source_id = :i"),
                        {"i": uuid.UUID(sid)},
                    )
                )
                .mappings()
                .one()
            )
        assert str(raw["body"]).startswith("aeg1s:") and raw["body_sealed"]

        items = await store.list_items(owner_id=owner, source_ref=sid[:8], limit=5, cipher=cipher)
        assert len(items) == 1 and items[0].body == "тело про важное"
        assert items[0].title == "Заголовок"  # заголовок открыт — по нему ищется без ключей

        # статусы: new → mark_pushed → pushed; mark_read закрывает всё
        assert await store.unread(owner_id=owner) == 1
        await store.mark_pushed(ids=[items[0].id])
        assert await store.unread(owner_id=owner) == 0
        assert (
            await store.list_items(
                owner_id=owner, source_ref=sid[:8], status="pushed", limit=5, cipher=cipher
            )
        )[0].status == "pushed"
        assert await store.mark_read(owner_id=owner, source_ref=sid[:8]) >= 0

    async def test_query_and_prune(self, owner: int) -> None:
        store = SqlParsingStore()
        target = f"https://example.org/feed-{uuid.uuid4().hex[:8]}.xml"
        sid = await store.add_source(
            owner_id=owner, target=target, kind="rss", label="q", interval_sec=600
        )
        async with session() as s:
            row = (
                (
                    await s.execute(
                        text("SELECT * FROM parsing.sources WHERE id = :i"), {"i": uuid.UUID(sid)}
                    )
                )
                .mappings()
                .one()
            )
        src = SourceRow(
            id=sid,
            owner_id=owner,
            kind=row["kind"],
            target=row["target"],
            label=row["label"],
            interval_sec=row["interval_sec"],
        )
        await store.add_items(
            source=src,
            items=[
                _draft("https://e/a", "про котов", "мяу"),
                _draft("https://e/b", "про псов", "гав"),
            ],
            cipher=None,
        )
        found = await store.list_items(owner_id=owner, query="кот", limit=10)
        assert [i.title for i in found] == ["про котов"]
        # prune: «состарившие» тела обнуляются, заголовки живут
        async with session() as s:
            await s.execute(
                text(
                    "UPDATE parsing.items SET first_seen = now() - interval '400 days'"
                    " WHERE source_id = :i"
                ),
                {"i": uuid.UUID(sid)},
            )
            await s.commit()
        assert await store.prune(keep_days=180) >= 2
        after = await store.list_items(owner_id=owner, source_ref=sid[:8], limit=10)
        assert len(after) == 2 and all(not i.body for i in after)
