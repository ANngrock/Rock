"""Меню поверх живой БД + контракт vision-схемы, на которую сядет TS-сервер.

Меню-рендереры проверены юнитами на подделках; здесь — что подделки не проверяют:
настоящие idStore-переходы (ref8 → строка), экранирование реальных данных и DDL-контракт
vision.sessions/vision.events (CHECK-переходы, CASCADE), потому что TS-сервер будет
вставлять ровно в эти таблицы и больше никуда.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text

from aegis.cognition.inbox import SqlInboxStore
from aegis.cognition.lexicon import SqlLexicon
from aegis.cognition.stickers import SqlStickers
from aegis.integrations.store import SqlConnectorStore
from aegis.interaction.telegram.menu import MenuDeps, apply_action, b64enc, perform_screen
from aegis.planning.nodes import SqlNodeStore
from aegis.planning.reminders import SqlReminderStore
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
                await s.execute(text("SELECT 1 FROM vision.sessions LIMIT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"миграция 0014 не накатана ({type(exc).__name__}) — `make migrate`")
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
                "DELETE FROM planning.reminders WHERE owner_id = :o",
                "DELETE FROM planning.node_commands WHERE owner_id = :o",
                "DELETE FROM planning.nodes WHERE owner_id = :o",
                "DELETE FROM integrations.connectors WHERE owner_id = :o",
                "DELETE FROM cognition.lexicon WHERE owner_id = :o",
                "DELETE FROM cognition.stickers WHERE owner_id = :o",
                "DELETE FROM cognition.inbox WHERE owner_id = :o",
                "DELETE FROM vision.events WHERE session_id IN"
                " (SELECT id FROM vision.sessions WHERE tg_user_id = :o)",
                "DELETE FROM vision.sessions WHERE tg_user_id = :o",
            ):
                await s.execute(text(stmt), {"o": value})
            await s.commit()


def _deps() -> MenuDeps:
    return MenuDeps(
        cfg=type(
            "C", (), {"telegram_miniapp_url": "", "userbot_enabled": True, "nodes_enabled": True}
        )(),
        reminders=SqlReminderStore(),
        nodes=SqlNodeStore(),
        connectors=SqlConnectorStore(),
        lexicon=SqlLexicon(),
        stickers=SqlStickers(),
        inbox=SqlInboxStore(),
        cost=None,
        now=datetime.now(UTC),
    )


async def test_reminders_roundtrip_through_menu(owner: int) -> None:
    store = SqlReminderStore()
    due = datetime.now(UTC) + timedelta(hours=3)
    rem_id = await store.add(owner_id=owner, body="купить <молоко> по списку", due_at=due)
    deps = _deps()
    text_, _kb_ = await perform_screen("reminders", owner, deps, can_control=True)
    assert rem_id[:8] in text_ and "&lt;молоко&gt;" in text_
    notice, screen = await apply_action("rem-cancel", b64enc(rem_id[:8]), owner, deps)
    assert (notice, screen) == ("✅ Снято", "reminders")
    text2, _ = await perform_screen("reminders", owner, deps, can_control=True)
    assert "Пусто" in text2


async def test_nodes_and_connectors_ref_resolution(owner: int) -> None:
    nodes = SqlNodeStore()
    name = f"меню-узел-{uuid.uuid4().hex[:6]}"
    node_id, _code = await nodes.enroll(owner_id=owner, name=name)
    conns = SqlConnectorStore()
    conn_name = (
        f"menu-tap-{uuid.uuid4().hex[:6]}"  # коннекторы: только строчная латиница — валидация
    )
    conn_id = await conns.add(owner_id=owner, kind="api", name=conn_name, config={})
    deps = _deps()
    text_, kb_ = await perform_screen("nodes", owner, deps, can_control=True)
    assert name in text_ and "⚪️" in text_  # pending — ещё не на привязке
    data = kb_.inline_keyboard[0][0].callback_data
    assert (
        data is not None and len(data.encode()) <= 64
    )  # реальное имя длиннее лимита — ref спасает
    notice, _ = await apply_action("node-revoke", b64enc(node_id[:8]), owner, deps)
    assert notice == "✅ Узел отвязан"
    text2, _ = await perform_screen("connectors", owner, deps, can_control=True)
    assert conn_name in text2 and "🟢" in text2
    notice2, _ = await apply_action("conn-toggle", b64enc(f"off|{conn_id[:8]}"), owner, deps)
    assert notice2 == "✅ Готово"
    rows = await conns.list(owner_id=owner)
    assert next(c for c in rows if c.name == conn_name).enabled is False


async def test_lexicon_stickers_inbox_screens(owner: int) -> None:
    await SqlLexicon().upsert(owner, "кр", "курсовая работа")
    await SqlStickers().add(
        owner, "обнимашки", "CAACAgIAAxkBAAEDabc123def456_обнимашки", ["sad", "joy"]
    )
    inbox = SqlInboxStore()
    uid = uuid.uuid4().hex[:10]
    await inbox.insert_if_new(
        owner_id=owner,
        daemon="testd",
        chat_id="-100500",
        chat_name="<Тест>",
        from_name="Тимур",
        msg="го <на> встречу",
        msg_uid=uid,
    )
    deps = _deps()
    lex_text, _ = await perform_screen("lexicon", owner, deps, can_control=False)
    assert "курсовая" in lex_text
    st_text, _ = await perform_screen("stickers", owner, deps, can_control=False)
    assert "обнимашки" in st_text and "sad" in st_text
    in_text, _ = await perform_screen("inbox", owner, deps, can_control=True)
    assert "&lt;Тест&gt;" in in_text and "stored" in in_text
    row = (await inbox.list_recent(owner_id=owner, limit=3))[0]
    assert row is not None
    notice, _ = await apply_action("inbox-discard", b64enc(str(row.id)[:8]), owner, deps)
    assert notice == "✅ Убрано"
    in2, kb2 = await perform_screen("inbox", owner, deps, can_control=True)
    # закрытая строка остаётся в ленте как факт, но кнопка действия с неё снимается
    assert "discarded" in in2
    labels = [b.text for row in kb2.inline_keyboard for b in row]
    assert not any("архив" in t.lower() for t in labels)


# ---------- контракт vision-схемы (его читает только TS — проверяем отсюда) ----------


async def test_vision_schema_contract(owner: int) -> None:
    async with session() as s:
        sid = (
            await s.execute(
                text(
                    "INSERT INTO vision.sessions (tg_user_id, mode, engine) VALUES"
                    " (:o, 'stream', 'gpt-4o-mini') RETURNING id::text"
                ),
                {"o": owner},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO vision.events (session_id, kind, text, ok, ms) VALUES"
                " (:s, 'analysis', :t, true, 812)"
            ),
            {"s": sid, "t": "на столе — ноутбук и кружка"},
        )
        await s.execute(
            text(
                "UPDATE vision.sessions SET frames = frames + 1, analyses = analyses + 1"
                " WHERE id = CAST(:s AS uuid)"
            ),
            {"s": sid},
        )
        await s.commit()
        row = (
            (
                await s.execute(
                    text(
                        "SELECT frames, analyses FROM vision.sessions WHERE id = CAST(:s AS uuid)"
                    ),
                    {"s": sid},
                )
            )
            .mappings()
            .one()
        )
        assert dict(row) == {"frames": 1, "analyses": 1}
        # CHECK злостно бережёт алфавиты
        with pytest.raises(Exception, match="events_kind_check"):  # имя дал сам Postgres
            await s.execute(
                text(
                    "INSERT INTO vision.events (session_id, kind) VALUES (CAST(:s AS uuid),"
                    " 'hentai_frames')"
                ),
                {"s": sid},
            )
        await s.rollback()
        with pytest.raises(Exception, match="sessions_mode_check"):
            await s.execute(
                text("INSERT INTO vision.sessions (tg_user_id, mode) VALUES (:o, 'godmode')"),
                {"o": owner},
            )
        await s.rollback()
        # каскад: удалил сессию — события исчезли (сырых кадров у нас всё равно нет)
        await s.execute(text("DELETE FROM vision.sessions WHERE id = CAST(:s AS uuid)"), {"s": sid})
        await s.commit()
        n = (
            await s.execute(
                text("SELECT count(*) FROM vision.events WHERE session_id = CAST(:s AS uuid)"),
                {"s": sid},
            )
        ).scalar_one()
        assert n == 0


async def test_screen_errors_do_not_escape(owner: int) -> None:
    # данные нет, но чужой owner не должен ничего увидеть: экран пуст, а не «ошибочка»
    deps = _deps()
    foreign = owner + 777_001
    text_, _ = await perform_screen("reminders", foreign, deps, can_control=False)
    assert "Пусто" in text_
    notice, screen = await apply_action("rem-cancel", b64enc("deadbeef"), foreign, deps)
    assert notice == "! не нашёл (уже ушло?)" and screen == "reminders"
