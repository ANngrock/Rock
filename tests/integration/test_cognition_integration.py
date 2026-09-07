"""Когнитивный слой на живом Postgres: идемпотентность инбокса, переходы статусов, UNIQUE.

Эвристики проверены юнитами; здесь — то, что живёт только в БД: ON CONFLICT-перезапись
термина, «один вердикт на сообщение» (UNIQUE owner+chat+uid), защита от двойного
set_assessment через `AND verdict='new'` и что mark() не переезжает закрытые строки.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text

from aegis.cognition.affect import SqlAffectStore
from aegis.cognition.inbox import SqlInboxStore, SqlUserbots
from aegis.cognition.journal import log_voice
from aegis.cognition.lexicon import SqlLexicon
from aegis.cognition.stickers import SqlStickers
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
                await s.execute(text("SELECT 1 FROM cognition.lexicon LIMIT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"миграция 0013 не накатана ({type(exc).__name__}) — `make migrate`")
        yield
        reset_engine()


@pytest_asyncio.fixture
async def owner(db: None) -> AsyncIterator[int]:
    value = int(uuid.uuid4().int % 2_000_000_000) + 10_000
    try:
        yield value
    finally:
        async with session() as s:
            for stmt in (
                "DELETE FROM cognition.lexicon WHERE owner_id = :o",
                "DELETE FROM cognition.affect_state WHERE owner_id = :o",
                "DELETE FROM cognition.voice_log WHERE owner_id = :o",
                "DELETE FROM cognition.stickers WHERE owner_id = :o",
                "DELETE FROM cognition.chat_policies WHERE owner_id = :o",
                "DELETE FROM cognition.inbox WHERE owner_id = :o",
                "DELETE FROM cognition.userbots WHERE owner_id = :o",
            ):
                await s.execute(text(stmt), {"o": value})
            await s.commit()


# ---------- лексикон ----------


async def test_lexicon_roundtrip_and_scoping(owner: int) -> None:
    lex = SqlLexicon()
    await lex.upsert(owner, "  Го   ", "давай, сейчас")
    await lex.upsert(owner, "Тимур", "электрик", kind="person")
    entries = await lex.list_terms(owner)
    assert [e.term for e in entries] == ["го", "тимур"]  # сортировка + нижний регистр
    assert await lex.upsert(owner, "го", "давай-обновлённое") == "го"  # ON CONFLICT перезапись
    entries = await lex.list_terms(owner)
    assert next(e for e in entries if e.term == "го").means == "давай-обновлённое"
    assert [e.kind for e in entries] == ["term", "person"]
    other = owner + 1
    assert await lex.list_terms(other) == []  # чужой словарь не виден
    assert await lex.remove(owner, "ГО") is True  # нормализация и на удалении
    assert await lex.remove(owner, "го") is False
    with pytest.raises(ValueError, match="kind"):
        await lex.upsert(owner, "x", "y", kind="magic")


# ---------- эмоциональный контур ----------


async def test_affect_accumulates_and_survives(owner: int) -> None:
    store = SqlAffectStore()
    # одиночная реплика силы не даёт (0.7×дельта), накопленная — даёт: ровно так заведено
    first = await store.signal(owner, {"valence": -0.6, "arousal": 0.4, "frustration": 0.7})
    assert first.mood == "irritated" and first.turns == 1
    current, since = await store.current(owner)
    assert since is not None and current.frustration == pytest.approx(first.frustration)
    second = await store.signal(owner, {"valence": 0.0, "arousal": 0.0, "frustration": 0.3})
    assert second.frustration > first.frustration and second.turns == 2
    # «пустая» реплика не двигает числа, но увеличивает счётчик хода — как у человека
    third = await store.signal(owner, {"valence": 0.0, "arousal": 0.0, "frustration": 0.0})
    assert third.frustration == pytest.approx(second.frustration) and third.turns == 3


# ---------- голосовой журнал ----------


async def test_voice_log_records_failures_too(owner: int) -> None:
    await log_voice(owner, direction="in", engine="whisper-1", transcription="привет", seconds=4)
    await log_voice(owner, direction="out", engine="tts-1", ok=False, error="эндпоинт недоступен")
    async with session() as s:
        rows = (
            (
                await s.execute(
                    text(
                        "SELECT direction, ok, error FROM cognition.voice_log WHERE owner_id = :o"
                        " ORDER BY created_at"
                    ),
                    {"o": owner},
                )
            )
            .mappings()
            .all()
        )
    assert [(r["direction"], r["ok"]) for r in rows] == [("in", True), ("out", False)]
    assert "недоступен" in rows[1]["error"]
    with pytest.raises(ValueError):
        await log_voice(owner, direction="middle", engine="x")


# ---------- стикеры ----------


async def test_stickers_upsert_and_validation(owner: int) -> None:
    st = SqlStickers()
    await st.add(owner, "праздник", "CAACAgIAAxk" + "x" * 40, ["Joy", "joy", "anxious"])
    items = await st.list_stickers(owner)
    assert len(items) == 1 and items[0].moods == ("anxious", "joy")  # дедуп + lower + сортировка
    await st.add(owner, "праздник", "CAACAgIAAxk" + "y" * 40, [])
    items = await st.list_stickers(owner)
    assert items[0].moods == ()  # ON CONFLICT обновил и теги
    with pytest.raises(ValueError, match="file_id"):
        await st.add(owner, "кривой", "abc", [])
    assert await st.remove(owner, "ПРАЗДНИК") is True  # регистронезависимое имя
    assert await st.list_stickers(owner + 1) == []


# ---------- инбокс ----------


async def _mk_row(store: SqlInboxStore, owner: int, *, uid: str | None = None) -> str | None:
    return await store.insert_if_new(
        owner_id=owner,
        daemon="laptop-1",
        chat_id="-100123",
        chat_name="Мастерская",
        from_name="Тимур",
        msg="сможешь посмотреть счёт за свет?",
        msg_uid=uid or uuid.uuid4().hex[:12],
    )


async def test_inbox_dedup_and_assessment(owner: int) -> None:
    store = SqlInboxStore()
    row_id = await _mk_row(store, owner, uid="dup1")
    assert row_id is not None
    assert await _mk_row(store, owner, uid="dup1") is None  # повтор доставки демона — не дубль
    rows = await store.claim_unassessed(limit=50)
    mine = [r for r in rows if r.id == row_id]
    assert len(mine) == 1 and mine[0].daemon == "laptop-1"
    assert mine[0].verdict == "new" and mine[0].status == "stored"
    await store.set_assessment(row_id, "action_required", "вопрос с просьбой")
    await store.set_assessment(row_id, "noise", "вторая попытка")  # уже не 'new' — молча мимо
    fresh = await store.get_by_ref(owner_id=owner, ref=row_id[:8])
    assert (
        fresh is not None
        and fresh.verdict == "action_required"
        and fresh.reason == "вопрос с просьбой"
    )
    with pytest.raises(ValueError, match="verdict"):
        await store.set_assessment(row_id, "meh", "x")


async def test_inbox_status_machine(owner: int) -> None:
    store = SqlInboxStore()
    row_id = await _mk_row(store, owner)
    assert row_id is not None
    await store.set_reply(row_id, "посмотрю вечером", "draft")
    assert not await store.mark(owner_id=owner + 7, row_id=row_id, status="sent")  # чужой — нет
    assert await store.mark(owner_id=owner, row_id=row_id, status="sent")
    assert not await store.mark(owner_id=owner, row_id=row_id, status="discarded")  # закрыто
    with pytest.raises(ValueError, match="draft|blocked|sent"):
        await store.set_reply(row_id, "x", "mailed")
    sent = await store.get_by_ref(owner_id=owner, ref=row_id[:8])
    assert sent is not None and sent.status == "sent" and sent.reply == "посмотрю вечером"
    # list_recent видит владелец, чужой — нет
    assert await store.list_recent(owner_id=owner, limit=5)
    assert await store.list_recent(owner_id=owner + 7, limit=5) == []


async def test_inbox_owner_of_and_isolation(owner: int) -> None:
    store = SqlInboxStore()
    row_id = await _mk_row(store, owner)
    assert row_id is not None
    assert await store.owner_of(row_id) == owner
    assert await store.get_by_ref(owner_id=owner + 1, ref=row_id[:8]) is None


async def test_policies_owner_scope_and_validation(owner: int) -> None:
    store = SqlInboxStore()
    await store.policy_set(owner, " @Timur_Elektrik ", "auto", "только срочное")
    assert await store.policy_get(owner, "@timur_elektrik") == "auto"  # нормализация с обеих сторон
    rows = await store.policy_list(owner)
    assert rows == [{"peer": "@timur_elektrik", "mode": "auto", "note": "только срочное"}]
    await store.policy_set(owner, "@timur_elektrik", "watch")  # ON CONFLICT — смена режима
    assert await store.policy_get(owner, "@timur_elektrik") == "watch"
    assert await store.policy_get(owner + 1, "@timur_elektrik") is None  # чужие политики не видны
    with pytest.raises(ValueError, match="режим"):
        await store.policy_set(owner, "x", "yolo")
    assert await store.policy_remove(owner, "@TIMUR_elektrik") is True
    assert await store.policy_remove(owner, "@timur_elektrik") is False


async def test_userbot_heartbeat_freshness(owner: int) -> None:
    store = SqlUserbots()
    await store.touch("laptop-1", owner, {"dialogs": 12, "os": "Linux"})
    items = await store.daemons(owner)
    assert len(items) == 1 and items[0]["online"] is True and items[0]["daemon"] == "laptop-1"
    await store.touch("laptop-1", owner, {"dialogs": 13})
    async with session() as s:  # отматываем heartbeat — «демон уснул вместе с ноутбуком»
        await s.execute(
            text(
                "UPDATE cognition.userbots SET last_seen = now() - interval '10 minutes'"
                " WHERE daemon = 'laptop-1'"
            )
        )
        await s.commit()
    items = await store.daemons(owner)
    assert items[0]["online"] is False and items[0]["age_s"] > 500
    assert await store.daemons(owner + 1) == []  # чужие демоны не показываются
