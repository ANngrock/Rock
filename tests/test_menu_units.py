"""Юниты меню: grammar колбэков, экраны на фейковых хранилищах, экранирование, право управлять.

aiogram-объекты не мокаем: _kb строит настоящую разметку — если формат кнопок сломается,
тест увидит это раньше Telegram.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aiogram.types import InlineKeyboardMarkup

from aegis.interaction.telegram.menu import (
    MenuDeps,
    apply_action,
    b64dec,
    b64enc,
    main_keyboard,
    parse_cb,
    perform_screen,
)

_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


# ---------- grammar ----------


def test_parse_cb_shapes() -> None:
    assert parse_cb("m:go:nodes") == ("go", "nodes", "")
    assert parse_cb("m:back") == ("go", "main", "")
    assert parse_cb("m:act:rem-cancel:abc123") == ("act", "rem-cancel", "abc123")
    assert parse_cb("m:noop") == ("noop", "", "")
    assert parse_cb("ok:123") is None  # чужие колбэки (подтверждения) — не наши
    assert parse_cb("m:") is None
    assert parse_cb("m:act:only-action") is None
    assert parse_cb("m:go:a:b:c") is None  # arg cbase64-ится, двоеточий в grammar быть не может


def test_b64_arg_roundtrip() -> None:
    for name in ("узел: 1-й", "a:b:c", "", "😀", "x" * 300):
        enc = b64enc(name)
        assert ":" not in enc and "=" not in enc  # безопасно для split/telegram
        assert b64dec(enc) == name
    assert b64dec("!!битый!!") == ""


# ---------- конфигурация кнопок ----------


def test_main_keyboard_vision_button_only_with_url() -> None:
    rows = main_keyboard(miniapp_url="")
    flat = [b for row in rows for b in row]
    assert any(b.get("data") == "m:go:vision" for b in flat)
    assert not any("web_app" in b for b in flat)
    rows2 = main_keyboard(miniapp_url="https://v.example/app")
    flat2 = [b for row in rows2 for b in row]
    wa = [b for b in flat2 if "web_app" in b]
    assert len(wa) == 1 and wa[0]["web_app"] == "https://v.example/app"


def test_kb_builds_real_markup() -> None:
    from aegis.interaction.telegram.menu import _kb

    kb = _kb(main_keyboard(miniapp_url="https://x.example/a"))
    assert isinstance(kb, InlineKeyboardMarkup)
    btn = kb.inline_keyboard[0][0]
    assert btn.text and (btn.callback_data or btn.web_app)


# ---------- фейковые хранилища ----------


class FakeRem:
    def __init__(self) -> None:
        self.rem = []

    async def list_scheduled(self, *, owner_id: int, limit: int):
        return [r for r in self.rem if r.owner_id == owner_id][:limit]

    async def cancel(self, *, owner_id: int, ref: str):
        for i, r in enumerate(self.rem):
            if r.owner_id == owner_id and str(r.id).startswith(ref):
                return self.rem.pop(i)
        return None


class FakeNode:
    def __init__(self, name, status, last_seen=None):
        self.name, self.status, self.last_seen, self.id = name, status, last_seen, name + "-uuid"
        self.caps: dict = {}


class FakeNodes:
    def __init__(self) -> None:
        self.nodes = [FakeNode("desk:1", "paired", _NOW - timedelta(seconds=30))]

    async def list_nodes(self, *, owner_id: int):
        return self.nodes

    async def revoke(self, *, owner_id: int, name: str):
        self.nodes = [n for n in self.nodes if n.name != name]
        return True


class FakeConn:
    def __init__(self) -> None:
        self.items = [
            SimpleNamespace(name="метро", kind="api", enabled=True, last_error="", id="c1")
        ]
        self.calls: list = []

    async def list(self, *, owner_id: int, enabled_only: bool = False):
        return self.items

    async def set_enabled(self, *, owner_id: int, name: str, enabled: bool):
        self.calls.append((name, enabled))
        return True


class FakeInbox:
    def __init__(self) -> None:
        self.rows = [
            SimpleNamespace(
                id="aaaa1111-2222",
                chat_id="-100",
                chat_name="Мастерская",
                from_name="Тимур",
                text="сможешь?",
                verdict="action_required",
                status="draft",
                reply="да",
                reason="вопрос",
                created_at=_NOW,
            )
        ]

    async def list_recent(self, *, owner_id: int, limit: int):
        return self.rows[:limit]

    async def get_by_ref(self, *, owner_id: int, ref: str):
        return next((r for r in self.rows if r.id.startswith(ref)), None)

    async def mark(self, *, owner_id: int, row_id: str, status: str):
        self.rows = []
        return True


class FakeCost:
    async def snapshot(self):
        return {
            "day": "07.09",
            "spent_usd": 0.5,
            "limit_usd": 2.0,
            "ratio": 0.25,
            "degradation_level": 0,
        }


def _deps(**kw) -> MenuDeps:
    base = dict(
        cfg=SimpleNamespace(
            timezone="Europe/Kiev",
            env="test",
            stream_replies=False,
            nodes_enabled=True,
            userbot_enabled=True,
            voice_enabled=False,
            integrations_enabled=True,
            telegram_miniapp_url="",
        ),
        reminders=FakeRem(),
        nodes=FakeNodes(),
        connectors=FakeConn(),
        lexicon=None,
        stickers=None,
        inbox=FakeInbox(),
        cost=FakeCost(),
        now=_NOW,
    )
    base.update(kw)
    return MenuDeps(**base)


def _rem(id_, owner_id, text):
    return SimpleNamespace(
        id=id_, owner_id=owner_id, text=text, due_at=_NOW + timedelta(hours=1), status="scheduled"
    )


# ---------- экраны ----------


async def test_reminders_screen_lists_and_escapes() -> None:
    deps = _deps()
    deps.reminders.rem = [_rem("abcd1234-uuid", 1, "<b>злом</b> не выведусь")]
    text, kb = await perform_screen("reminders", 1, deps, can_control=True)
    assert "abcd1234" in text
    assert "&lt;b&gt;злом&lt;/b&gt;" in text  # html в тексте напоминания обязан быть съеден
    assert "<b>злом</b>" not in text
    data = kb.inline_keyboard[0][0].callback_data
    assert data is not None and data.startswith("m:act:rem-cancel:")
    assert len(data.encode()) <= 64  # лимит Telegram на callback_data
    assert kb.inline_keyboard[-1][0].text == "⟳ Обновить"


async def test_no_control_no_action_buttons() -> None:
    deps = _deps()
    deps.reminders.rem = [_rem("abcd1234-uuid", 1, "тест")]
    text, kb = await perform_screen("reminders", 1, deps, can_control=False)
    assert "✖️" not in "".join(b.text for row in kb.inline_keyboard for b in row)


async def test_nodes_online_marks() -> None:
    deps = _deps()
    text, kb = await perform_screen("nodes", 1, deps, can_control=True)
    assert "🟢" in text and "desk:1" in text
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("🔌 Отвязать" in t for t in labels)
    # отвязка по b64-имени доходит до хранилища
    # ref: первые 8 символов id — имя в payload не влезло бы (лимит 64 байта)
    notice, screen = await apply_action("node-revoke", b64enc("desk:1-uuid"[:8]), 1, deps)
    assert notice == "✅ Узел отвязан" and deps.nodes.nodes == [] and screen == "nodes"
    notice2, _ = await apply_action("node-revoke", b64enc("nope"), 1, deps)
    assert notice2 == "! не нашёл (уже отвязан?)"


async def test_inbox_discard_action() -> None:
    deps = _deps()
    text, _ = await perform_screen("inbox", 1, deps, can_control=True)
    assert "✍️" in text and "Мастерская" in text
    notice, screen = await apply_action("inbox-discard", b64enc("aaaa1111"), 1, deps)
    assert notice == "✅ Убрано" and screen == "inbox"
    assert deps.inbox.rows == []


async def test_conn_toggle_roundtrip() -> None:
    deps = _deps()
    text, kb = await perform_screen("connectors", 1, deps, can_control=True)
    assert "🟢" in text and "1/1" in text
    data = kb.inline_keyboard[0][0].callback_data
    assert data is not None and len(data.encode()) <= 64
    notice, screen = await apply_action("conn-toggle", b64enc("off|c1"), 1, deps)
    assert deps.connectors.calls == [("метро", False)]
    assert notice == "✅ Готово" and screen == "connectors"
    notice2, _ = await apply_action("conn-toggle", b64enc("on|zzz"), 1, deps)
    assert notice2 == "! коннектор не найден (обнови экран)"


async def test_rem_cancel_then_refresh_notice() -> None:
    deps = _deps()
    deps.reminders.rem = [_rem("ffff0000-id", 1, "позвонить")]
    notice, screen = await apply_action("rem-cancel", b64enc("ffff0000"), 1, deps)
    assert notice == "✅ Снято" and screen == "reminders"
    notice2, _ = await apply_action("rem-cancel", b64enc("ffff0000"), 1, deps)
    assert notice2 == "! не нашёл (уже ушло?)"


async def test_refresh_semantics() -> None:
    deps = _deps()
    assert await apply_action("refresh", "nodes", 1, deps) == ("", "nodes")
    assert await apply_action("refresh", "", 1, deps) == ("", "main")


async def test_cost_and_status_screens() -> None:
    deps = _deps()
    text, _ = await perform_screen("cost", 1, deps, can_control=False)
    assert "0.500" in text and "25%" in text and "▮▮" in text
    text2, _ = await perform_screen("status", 1, deps, can_control=False)
    assert "узлов: 1" in text2 and "деградация" in text2


async def test_broken_store_degrades_not_crashes() -> None:
    class Boom:
        async def list_scheduled(self, **kw):
            raise RuntimeError("база легла")

        async def cancel(self, **kw):
            raise RuntimeError

    deps = _deps(reminders=Boom())
    text, kb = await perform_screen("reminders", 1, deps, can_control=True)
    assert "Пусто" in text  # _gather поймал: экран открылся с пустым списком
    deps2 = _deps(cost=Boom())  # у Boom нет snapshot: AttributeError ловит общий щит
    text2, _ = await perform_screen("status", 1, deps2, can_control=False)
    assert "⚠️ Экран недоступен" in text2  # меню докладывает, а не падает
    text3, _ = await perform_screen("main", 1, deps2, can_control=False)
    assert "Aegis" in text3  # главный экран не зависит от хранилищ вообще


async def test_vision_screen_without_url_and_about() -> None:
    deps = _deps()
    text, _ = await perform_screen("vision", 1, deps, can_control=True)
    assert "TELEGRAM_MINIAPP_URL" in text
    deps.cfg.telegram_miniapp_url = "https://v.example"
    text2, kb2 = await perform_screen("vision", 1, deps, can_control=True)
    assert kb2.inline_keyboard[0][0].web_app.url == "https://v.example"
    text3, _ = await perform_screen("about", 1, deps, can_control=True)
    assert "/help" in text3 and "Aegis" in text3


async def test_unknown_screen_and_action() -> None:
    deps = _deps()
    text, _ = await perform_screen("wat", 1, deps, can_control=False)
    assert "Такого экрана нет" in text
    notice, screen = await apply_action("wat", "", 1, deps)
    assert notice == "! неизвестное действие (обновите бота)" and screen == "main"


async def test_empty_screens_show_honest_hints() -> None:
    deps = _deps(lexicon=None, stickers=None)
    deps.reminders.rem = []
    deps.nodes.nodes = []
    deps.connectors.items = []
    deps.inbox.rows = []
    hints = {"reminders": "Пусто", "nodes": "Ни одного", "connectors": "Пусто", "inbox": "пуст"}
    for screen, hint in hints.items():
        text, _ = await perform_screen(screen, 1, deps, can_control=True)
        assert hint in text, screen
