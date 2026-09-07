"""Меню бота: навигация кнопками поверх того, что и так живёт в БД.

Принципы, из которых собран этот файл:

* меню — витрина, а не вторая логика: каждый экран рендерится из существующих хранилищ,
  никаких «меню-состояний» и своих копий данных. Упало хранилище — экран честно говорит «⚠️»;
* аргументы кнопок — base64url: имена узлов и коннекторов бывают с двоеточием, кириллицей и
  пробелами, а grammar `m:act:<действие>:<arg>` обязана ломаться от этого НЕ имеет права;
* право навать действия есть только владельцу: просмотр — всем пущенным (их впустил OwnerOnly),
  кнопки управления для не-владельца просто не рисуются, а на прямое нажатие — отказ на месте
  (ридер не доверяет отрисованному: чужой клиент мог показать то, чего не было);
* правки на месте (edit_message_text): меню — панель приборов, а не лента уведомлений.

Форматирование — то же «дорого», что и в ответах: заголовок, секции, счётчики, ровно один
смысловой эмодзи на строку.
"""

from __future__ import annotations

import asyncio
import base64
import html
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "MenuDeps",
    "apply_action",
    "b64dec",
    "b64enc",
    "main_keyboard",
    "parse_cb",
    "perform_screen",
]


# ---------- утилиты callback-grammar ----------


def b64enc(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def b64dec(token: str) -> str:
    pad = "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode(token + pad).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def parse_cb(data: str) -> tuple[str, str, str] | None:
    """m:<...> → (kind, screen|action, arg). Вне схемы — None (не наш колбэк)."""
    if not data.startswith("m:"):
        return None
    parts = data[2:].split(":", 2)
    if parts == ["back"]:
        return ("go", "main", "")
    if parts[0] == "go" and len(parts) == 2:  # noqa: PLR2004 — формат go:<screen>
        return ("go", parts[1], "")
    if parts[0] == "act" and len(parts) == 3:  # noqa: PLR2004 — формат act:<действие>:<arg>
        return ("act", parts[1], parts[2])
    if parts == ["noop"]:
        return ("noop", "", "")
    return None


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


# ---------- зависимости экранов ----------


@dataclass(slots=True)
class MenuDeps:
    """Всё, что меню читает. В проде строится из App; в тестах — подделки с теми же методами."""

    cfg: Any
    reminders: Any = None
    nodes: Any = None
    connectors: Any = None
    lexicon: Any = None
    stickers: Any = None
    inbox: Any = None
    cost: Any = None
    feeds: Any = None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))


async def _gather(coro: Any, fallback: Any) -> Any:
    try:
        return await coro
    except Exception:  # noqa: BLE001 - экран обязан открыться и на мёртвой половинке системы
        return fallback


def _hhmm(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%d.%m %H:%M")


# ---------- главный экран ----------

_SECTIONS = (
    ("status", "🤖 Состояние"),
    ("cost", "💸 Расходы"),
    ("reminders", "⏰ Напоминания"),
    ("nodes", "💻 Узлы"),
    ("lexicon", "🧠 Словарь"),
    ("stickers", "🎭 Стикер-коллекция"),
    ("inbox", "💬 Личные чаты"),
    ("connectors", "🔌 Подключения"),
    ("feeds", "📡 Парсер"),
)


def _btn(text: str, data: str) -> dict[str, str]:
    return {"text": text, "data": data}


def main_keyboard(*, miniapp_url: str = "") -> list[list[dict[str, str]]]:
    """Сетка 2×N + строка Vision. Кнопка-приложение отдельно: у неё свой объект, не callback."""
    rows: list[list[dict[str, str]]] = []
    for i in range(0, len(_SECTIONS), 2):
        row = [_btn(t, f"m:go:{name}") for name, t in _SECTIONS[i : i + 2]]  # noqa: E203
        if len(row) == 1:
            row.append(_btn("🤝 О боте", "m:go:about"))
        rows.append(row)
    vision = (
        {"text": "📷 Зрение — открыть камеру", "web_app": miniapp_url}
        if miniapp_url
        else _btn("📷 Зрение", "m:go:vision")
    )
    rows.append([vision, _btn("✖️ Закрыть", "m:noop")])
    return rows


def _kb(rows: list[list[dict[str, str]]]) -> Any:
    """Собрать aiogram-разметку лениво: тесты и CLI работают без построенного Bot."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

    out = []
    for row in rows:
        btns = []
        for spec in row:
            if "web_app" in spec:
                btns.append(
                    InlineKeyboardButton(text=spec["text"], web_app=WebAppInfo(url=spec["web_app"]))
                )
            else:
                btns.append(InlineKeyboardButton(text=spec["text"], callback_data=spec["data"]))
        out.append(btns)
    return InlineKeyboardMarkup(inline_keyboard=out)


def _back_row(screen: str = "main") -> list[dict[str, str]]:
    rows = [_btn("⟳ Обновить", f"m:act:refresh:{screen}")]
    if screen != "main":
        rows.append(_btn("« В меню", "m:back"))
    return rows


def _page(
    title: str,
    body: str,
    *,
    controls: list[list[dict[str, str]]] | None,
    screen: str,
) -> tuple[str, Any]:
    rows: list[list[dict[str, str]]] = list(controls or [])
    rows.append(_back_row(screen))
    return f"<b>{title}</b>\n{body}", _kb(rows)


# ---------- экраны ----------


async def perform_screen(
    screen: str, owner_id: int, deps: MenuDeps, *, can_control: bool
) -> tuple[str, Any]:
    """Экран целиком: данные тянутся от сюда, сбои хранилищ не выпрыгивают наружу."""
    if screen == "refresh":
        screen = "main"
    if screen == "main":
        return _main_page(deps, owner_id)
    try:
        if screen == "status":
            return await _screen_status(owner_id, deps)
        if screen == "cost":
            return await _screen_cost(owner_id, deps)
        if screen == "reminders":
            return await _screen_reminders(owner_id, deps, can_control)
        if screen == "nodes":
            return await _screen_nodes(owner_id, deps, can_control)
        if screen == "lexicon":
            return await _screen_lexicon(owner_id, deps)
        if screen == "stickers":
            return await _screen_stickers(owner_id, deps)
        if screen == "inbox":
            return await _screen_inbox(owner_id, deps, can_control)
        if screen == "connectors":
            return await _screen_connectors(owner_id, deps, can_control)
        if screen == "feeds":
            return await _screen_feeds(owner_id, deps, can_control)
        if screen == "vision":
            return _screen_vision(deps)
        if screen == "about":
            return _screen_about(deps)
    except Exception as exc:  # noqa: BLE001 - меню не падает: меню докладывает
        return _page(
            "⚠️ Экран недоступен",
            f"Данные не читаются: <code>{_esc(type(exc).__name__)}</code>",
            controls=None,
            screen=screen,
        )
    return _page("Меню", "Такого экрана нет (обновите бота?).", controls=None, screen="main")


def _main_page(deps: MenuDeps, owner_id: int) -> tuple[str, Any]:
    flags = [
        f"{'✅' if getattr(deps.cfg, 'nodes_enabled', False) else '⚪️'} узлы",
        f"{'✅' if getattr(deps.cfg, 'userbot_enabled', False) else '⚪️'} личные чаты",
        f"{'✅' if getattr(deps.cfg, 'voice_enabled', False) else '⚪️'} голос",
        f"{'✅' if getattr(deps.cfg, 'integrations_enabled', False) else '⚪️'} подключения",
    ]
    body = (
        "Выбери раздел. Все данные живут в базе — меню только показывает их красиво.\n\n"
        + " · ".join(flags)
    )
    rows = main_keyboard(miniapp_url=str(getattr(deps.cfg, "telegram_miniapp_url", "") or ""))
    return f"<b>🛰 Aegis — меню</b>\n{body}", _kb(rows)


async def _screen_status(owner_id: int, deps: MenuDeps) -> tuple[str, Any]:
    cfg = deps.cfg
    snap = await _gather(deps.cost.snapshot() if deps.cost else None, None)
    deg = snap.get("degradation_level", 0) if isinstance(snap, dict) else "?"
    shown = await _gather(_counts(deps, owner_id), ("не читается", "не читается", "не читается"))
    rem_n, node_n, inbox_n = shown
    lines = [
        f"• время: <i>{_esc(str(getattr(cfg, 'timezone', '')))}</i> · окружение:"
        f" <code>{_esc(str(getattr(cfg, 'env', '?')))}</code>",
        f"• деградация LLM: <b>{deg}</b> (0 — норма)",
        f"• напоминаний в плане: {rem_n} · узлов: {node_n} · открытых чатов: {inbox_n}",
        f"• стриминг ответов: {'включён' if getattr(cfg, 'stream_replies', False) else 'выключен'},"
        f" подтверждения записей — по политике",
    ]
    return _page("🤖 Состояние", "\n".join(lines), controls=None, screen="status")


async def _counts(deps: MenuDeps, owner_id: int) -> tuple[Any, Any, Any]:
    rem = await deps.reminders.list_scheduled(owner_id=owner_id, limit=100)
    nodes = await deps.nodes.list_nodes(owner_id=owner_id)
    inbox = await deps.inbox.list_recent(owner_id=owner_id, limit=100)
    return len(rem), sum(1 for n in nodes if n.status == "paired"), len(inbox)


async def _screen_cost(owner_id: int, deps: MenuDeps) -> tuple[str, Any]:
    snap = await _gather(deps.cost.snapshot() if deps.cost else None, None)
    if not isinstance(snap, dict):
        return _page(
            "💸 Расходы", "Счётчик недоступен (redis/процесс).", controls=None, screen="cost"
        )
    ratio = float(snap.get("ratio") or 0)
    bar_n = min(10, int(round(ratio * 10)))
    bar = "▮" * bar_n + "▯" * (10 - bar_n)
    body = (
        f"Сегодня <code>${float(snap.get('spent_usd') or 0):.3f}</code> из "
        f"<code>${float(snap.get('limit_usd') or 0):.2f}</code> {bar} ({ratio * 100:.0f}%)\n"
        f"Уровень деградации: <b>{snap.get('degradation_level', 0)}</b>"
    )
    return _page("💸 Расходы на модели", body, controls=None, screen="cost")


async def _screen_reminders(owner_id: int, deps: MenuDeps, can_control: bool) -> tuple[str, Any]:
    rows = await _gather(
        deps.reminders.list_scheduled(owner_id=owner_id, limit=8) if deps.reminders else None, []
    )
    if not rows:
        return _page(
            "⏰ Напоминания",
            "Пусто. Скажи боту «напомни в 18:00 …» — появится здесь.",
            controls=None,
            screen="reminders",
        )
    lines, controls = [], []
    for r in rows:
        lines.append(f"• <code>{str(r.id)[:8]}</code> {_hhmm(r.due_at)} — {_esc(r.text[:80])}")
        if can_control:
            controls.append([_btn(f"✖️ {r.text[:24]}", f"m:act:rem-cancel:{b64enc(str(r.id)[:8])}")])
    return _page("⏰ Намечено", "\n".join(lines), controls=controls or None, screen="reminders")


async def _screen_nodes(owner_id: int, deps: MenuDeps, can_control: bool) -> tuple[str, Any]:
    nodes = await _gather(deps.nodes.list_nodes(owner_id=owner_id) if deps.nodes else None, [])
    if not nodes:
        return _page(
            "💻 Узлы",
            "Ни одного. Привязка: <code>aegis node enroll ИМЯ</code>, затем на машине —"
            " <code>aegis node serve</code>.",
            controls=None,
            screen="nodes",
        )
    lines, controls = [], []
    for n in nodes:
        age = (deps.now - n.last_seen).total_seconds() if n.last_seen is not None else None
        seen = (
            "никогда"
            if age is None
            else (f"{int(age)}с назад" if age < 3600 else f"{int(age // 60)}м назад")
        )  # noqa: PLR2004
        online = n.status == "paired" and age is not None and age < 90  # noqa: PLR2004 — как ONLINE_STALE
        mark = "🟢" if online else ("🟡" if n.status == "paired" else "⚪️")
        lines.append(f"{mark} <b>{_esc(n.name)}</b> — {n.status}, heartbeat {seen}")
        if can_control and n.status in ("paired", "pending"):
            # ref, а не имя: callback_data живёт в лимите 64 байта, а имена — нет
            controls.append(
                [_btn(f"🔌 Отвязать {n.name[:20]}", f"m:act:node-revoke:{b64enc(str(n.id)[:8])}")]
            )
    return _page("💻 Узлы", "\n".join(lines), controls=controls or None, screen="nodes")


async def _screen_lexicon(owner_id: int, deps: MenuDeps) -> tuple[str, Any]:
    entries = await _gather(deps.lexicon.list_terms(owner_id) if deps.lexicon else None, [])
    if not entries:
        return _page(
            "🧠 Словарь",
            "Пусто. «запомни: кр = курсовая» — и термин будет узнаваться.",
            controls=None,
            screen="lexicon",
        )
    lines = [f"<b>{_esc(e.term)}</b> = {_esc(e.means[:120])} <i>({e.kind})</i>" for e in entries]
    return _page(f"🧠 Словарь · {len(entries)}", "\n".join(lines), controls=None, screen="lexicon")


async def _screen_stickers(owner_id: int, deps: MenuDeps) -> tuple[str, Any]:
    items = await _gather(deps.stickers.list_stickers(owner_id) if deps.stickers else None, [])
    if not items:
        return _page(
            "🎭 Стикер-коллекция",
            "Пусто. <code>aegis sticker add имя FILE_ID --moods joy,sad</code> — и реакции"
            " появятся в ответах.",
            controls=None,
            screen="stickers",
        )
    lines = [
        f"• {_esc(st.name)} — {', '.join(st.moods) if st.moods else 'любое настроение'}"
        for st in items
    ]
    return _page(
        f"🎭 Стикер-коллекция · {len(items)}", "\n".join(lines), controls=None, screen="stickers"
    )


async def _screen_feeds(owner_id: int, deps: MenuDeps, can_control: bool) -> tuple[str, Any]:
    if not getattr(deps.cfg, "parser_enabled", True):
        return _page(
            "📡 Парсер",
            "Выключен (<code>AEGIS_PARSER_ENABLED=false</code>). Включи — и скажи боту «следи"
            " за каналом @…»: страницы, RSS/Atom/JSON-ленты и публичные t.me под одним движком.",
            controls=None,
            screen="feeds",
        )
    rows = await _gather(
        deps.feeds.list_sources(owner_id=owner_id, limit=12) if deps.feeds else None, []
    )
    unread = await _gather(deps.feeds.unread(owner_id=owner_id) if deps.feeds else None, 0)
    if not rows:
        return _page(
            "📡 Парсер · пусто",
            "Наблюдений нет. Скажи мне «следи за @durov» или за любой страницей/лентой —"
            " заведу; тела при этом лежат запечатанными (поворот ключа — каждые 2 минуты).",
            controls=[[_btn("⟳ Проверить созревшие", "m:act:feed-run:")]],
            screen="feeds",
        )
    lines = []
    controls = []
    for src in rows:
        mark = "🟢" if src.enabled else "⏸"
        err = f"\n     ⚠️ {_esc(src.last_error[:90])}" if src.last_error else ""
        was = f" · читали {_hhmm(src.last_check)}" if src.last_check else ""
        lines.append(
            f"{mark} <b>{_esc(src.label or src.target)}</b> · {src.kind} · каждые"
            f" {src.interval_sec // 60}м · следующий прогон по графику{was}{err}"
        )
        if can_control:
            verb = "⏸" if src.enabled else "▶"
            controls.append(
                [
                    _btn(
                        f"{verb} {src.label or src.target}"[:64],
                        f"m:act:feed-toggle:{b64enc(src.id[:8])}",
                    )
                ]
            )
    if can_control:
        controls.insert(0, [_btn("✔️ Отметить прочитанным", "m:act:feed-readall:")])
    controls.insert(0, [_btn("⟳ Проверить созревшие сейчас", "m:act:feed-run:")])
    fresh = f" · ✉️ {unread}" if unread else ""
    return _page(
        f"📡 Парсер · {len(rows)}{fresh}", "\n".join(lines), controls=controls, screen="feeds"
    )


async def _screen_inbox(owner_id: int, deps: MenuDeps, can_control: bool) -> tuple[str, Any]:
    if not getattr(deps.cfg, "userbot_enabled", False):
        return _page(
            "💬 Личные чаты",
            "Мост выключен (<code>USERBOT_ENABLED=false</code>). Демон запускается на твоей"
            " машине: <code>aegis userbot serve</code>.",
            controls=None,
            screen="inbox",
        )
    rows = await _gather(
        deps.inbox.list_recent(owner_id=owner_id, limit=6) if deps.inbox else None, []
    )
    if not rows:
        return _page(
            "💬 Личные чаты",
            "Инбокс пуст — демон ещё ничего не принёс.",
            controls=None,
            screen="inbox",
        )
    lines, controls = [], []
    for r in rows:
        chip = {
            "noise": "·",
            "info": "ℹ️",
            "important": "⚠️",
            "action_required": "✍️",
            "urgent": "‼️",
        }.get(r.verdict, "·")
        draft = " · ✍ черновик" if r.reply else ""
        line = f"{chip} <code>{str(r.id)[:8]}</code> {_esc(r.chat_name or r.chat_id)} — {r.status}"
        lines.append(line + draft)
        if can_control and r.status in ("stored", "notified", "draft"):
            controls.append(
                [_btn(f"🗂 В архив {str(r.id)[:8]}", f"m:act:inbox-discard:{b64enc(str(r.id)[:8])}")]
            )
    return _page(
        "💬 Последние входящие", "\n".join(lines), controls=controls or None, screen="inbox"
    )


async def _screen_connectors(owner_id: int, deps: MenuDeps, can_control: bool) -> tuple[str, Any]:
    rows = await _gather(deps.connectors.list(owner_id=owner_id) if deps.connectors else None, [])
    if not rows:
        return _page(
            "🔌 Подключения",
            "Пусто. <code>aegis connect add-mcp …</code> — и сервер встанет в этот список.",
            controls=None,
            screen="connectors",
        )
    lines, controls = [], []
    for c in rows:
        mark = "🟢" if c.enabled and not c.last_error else ("🟡" if c.enabled else "⚪️")
        err = f" — <i>{_esc(c.last_error[:60])}</i>" if c.last_error else ""
        lines.append(f"{mark} <b>{_esc(c.name)}</b> <i>({c.kind})</i>{err}")
        if can_control:
            verb = "⏸ Выключить" if c.enabled else "▶️ Включить"
            payload = b64enc(("off" if c.enabled else "on") + "|" + str(c.id)[:8])
            controls.append([_btn(f"{verb} {c.name[:20]}", f"m:act:conn-toggle:{payload}")])
    return _page(
        f"🔌 Подключения · {sum(1 for c in rows if c.enabled)}/{len(rows)}",
        "\n".join(lines),
        controls=controls or None,
        screen="connectors",
    )


def _screen_about(deps: MenuDeps) -> tuple[str, Any]:
    env = _esc(str(getattr(deps.cfg, "env", "?")))
    body = (
        "Aegis — приватный ассистент-система: напоминания, узлы, голос, личные чаты, подключения\n"
        "· всё в твоей базе, ничего не торчит наружу, кроме ответов тебе\n"
        "· команды: <code>/help</code> покажет список, <code>/menu</code> — вернуться сюда\n"
        f"· окружение: <code>{env}</code>\n"
        "\n"
        "Хочешь кадр с камеры на анализ — кнопка «📷 Зрение»."
    )
    return _page("🤝 О боте", body, controls=None, screen="about")


def _screen_vision(deps: MenuDeps) -> tuple[str, Any]:
    url = str(getattr(deps.cfg, "telegram_miniapp_url", "") or "")
    if url:
        body = (
            "Мини-апп настроен. Нажми «📷 Открыть камеру» — сервер возьмёт поток, будет слать"
            " кадры в модель зрения и показывать анализ в реальном времени."
        )
        kb = _kb([[{"text": "📷 Открыть камеру", "web_app": url}], _back_row("vision")])
        return "<b>📷 Зрение</b>\n" + body, kb
    body = (
        "Мини-апп не настроен. На сервере: <code>miniapp/</code> — <code>npm install && npm run"
        " build</code>, поднять <code>docker compose --profile miniapp up -d vision</code>,"
        " и указать <code>TELEGRAM_MINIAPP_URL=https://…</code> в .env бота."
    )
    return _page("📷 Зрение", body, controls=None, screen="vision")


# ---------- действия ----------


async def apply_action(action: str, arg: str, owner_id: int, deps: MenuDeps) -> tuple[str, str]:
    """→ (уведомление для callback, какой экран перечитать). Право проверяет вызывающий —
    здесь проверяются данные, а не личность."""
    if action == "refresh":
        return ("", "main" if not arg or arg == "main" else arg)
    if action == "rem-cancel":
        ref = b64dec(arg)
        gone = await _gather(
            deps.reminders.cancel(owner_id=owner_id, ref=ref) if deps.reminders else None, None
        )
        return ("✅ Снято" if gone else "! не нашёл (уже ушло?)", "reminders")
    if action == "node-revoke":
        ref = b64dec(arg)
        nodes = await _gather(deps.nodes.list_nodes(owner_id=owner_id) if deps.nodes else None, [])
        node = next((n for n in nodes if str(n.id).startswith(ref)), None)
        if node is None:
            return ("! не нашёл (уже отвязан?)", "nodes")
        ok = await _gather(deps.nodes.revoke(owner_id=owner_id, name=node.name), False)
        return ("✅ Узел отвязан" if ok else "! не получилось", "nodes")
    if action == "inbox-discard":
        ref = b64dec(arg)
        row = await _gather(
            deps.inbox.get_by_ref(owner_id=owner_id, ref=ref) if deps.inbox else None, None
        )
        if row is None:
            return ("! строка не найдена", "inbox")
        ok = await _gather(
            deps.inbox.mark(owner_id=owner_id, row_id=row.id, status="discarded"), False
        )
        return ("✅ Убрано" if ok else "! уже закрыто", "inbox")
    if action == "feed-toggle":
        ref = b64dec(arg)
        rows = await _gather(
            deps.feeds.list_sources(owner_id=owner_id, limit=50) if deps.feeds else None, []
        )
        src = next((r for r in rows if str(r.id).startswith(ref)), None)
        if src is None:
            return ("! источник не найден (обнови экран)", "feeds")
        outcome = await _gather(
            deps.feeds.set_enabled(owner_id=owner_id, ref=src.id[:8], enabled=not src.enabled),
            None,
        )
        if outcome is None:
            return ("! не получилось (база жива?)", "feeds")
        return ("▶ Запущено" if not src.enabled else "⏸ Приостановлено", "feeds")
    if action == "feed-run":
        if deps.feeds is None:
            return ("! база недоступна", "feeds")
        try:
            from aegis.parsing.watcher import run_feeds
            from aegis.platform.vault import process_cipher

            report = await asyncio.wait_for(
                run_feeds(
                    deps.feeds,
                    owner_id=owner_id,
                    cfg=deps.cfg,
                    cipher=process_cipher(deps.cfg),
                    send=None,
                ),
                timeout=90.0,
            )
            summ = report.summary()
            return (
                f"Проверено {summ['checked']}, нового {summ['new_items']}, сбоев {summ['errors']}",
                "feeds",
            )
        except Exception as exc:  # noqa: BLE001 - экран докладывает, не падает
            return (f"! тик не вышел: {type(exc).__name__}", "feeds")
    if action == "feed-readall":
        n = await _gather(deps.feeds.mark_read(owner_id=owner_id) if deps.feeds else None, 0)
        return (f"✔️ Прочитано: {n}" if n else "И так всё прочитано", "feeds")
    if action == "conn-toggle":
        want, _, ref = b64dec(arg).partition("|")
        rows = await _gather(
            deps.connectors.list(owner_id=owner_id) if deps.connectors else None, []
        )
        row = next((c for c in rows if str(c.id).startswith(ref)), None)
        if row is None:
            return ("! коннектор не найден (обнови экран)", "connectors")
        ok = await _gather(
            deps.connectors.set_enabled(owner_id=owner_id, name=row.name, enabled=want == "on"),
            False,
        )
        return ("✅ Готово" if ok else "! не получилось", "connectors")
    return ("! неизвестное действие (обновите бота)", "main")
