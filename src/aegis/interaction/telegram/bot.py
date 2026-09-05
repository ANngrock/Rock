"""Telegram-бот — единственный интерфейс шага 1.

Принципы, которые здесь защищены кодом:

* **whitelist до диспатча**: ``OwnerOnly`` — outer middleware, чужие апдейты не доходят ни до
  какого хендлера (и не получают ответа: бот для одного человека не должен ничего подтверждать
  посторонним);
* **команды живут без LLM**: ``/cost``, ``/tools``, ``/new``, ``/halt`` работают при недоступной
  модели, исчерпанном бюджете и даже без БД (принцип 5);
* **подтверждения** — inline-кнопки поверх сообщения с диффом действия; решение принимает policy
  engine, интерфейс только показывает;
* **битый HTML** не виден владельцу: рендер чинит разметку, а при 400 от Telegram текст уходит
  экранированным.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import re
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from aegis.agents.supervisor import Inbound, Reply
from aegis.agents.tools import load_builtin_tools
from aegis.agents.tools.images import sniff_mime
from aegis.agents.tools.registry import Attachment
from aegis.interaction.telegram.render import render_for_telegram, strip_tags
from aegis.interaction.telegram.stream import PLACEHOLDER, make_stream
from aegis.platform.config import ConfigError
from aegis.platform.gateway.diagnose import redact_secrets
from aegis.runtime import App, build_app

__all__ = ["OwnerOnly", "build_dispatcher", "main"]

load_builtin_tools()

log = structlog.get_logger(__name__)

router = Router()

_HELP = (
    "<b>Aegis</b> на связи. Пиши свободно: спроси, попроси найти, записать, запомнить — "
    "можно кидать фото и ссылки.\n\n"
    "<b>Команды</b>\n"
    "• /new — начать диалог заново (долговременная память не трогается)\n"
    "• /cost — расходы на LLM сегодня\n"
    "• /status — режим, модели, бюджеты, kill switch\n"
    "• /tools — доступные инструменты\n"
    "• /replay [id] — повторить ход по журналу и сравнить ответ (без id — последний)\n"
    "• /halt — заморозить записи (бот отвечает только чтением)\n"
    "• /resume — разморозить записи\n"
    "• /help — это сообщение"
)


class OwnerOnly:
    """Пускаем ровно одного человека. Остальные — молча, без «я тебя не знаю»."""

    def __init__(self, owner_id: int) -> None:
        self._owner_id = owner_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        # сравниваем строки: любое «не похоже на владельца» = отказ, а не исключение в мидлвари
        user_id = getattr(user, "id", None)
        if user_id is None or str(user_id) != str(self._owner_id):
            log.warning("owner_only.rejected", user_id=getattr(user, "id", None))
            return None
        return await handler(event, data)


def _confirm_keyboard(pending_id: str, count: int) -> InlineKeyboardMarkup:
    label = "Выполнить" if count == 1 else f"Выполнить всё ({count})"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"✅ {label}", callback_data=f"ok:{pending_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"no:{pending_id}"),
            ]
        ]
    )


async def send_reply(bot: Bot, chat_id: int, reply: Reply) -> Message | None:
    """Отправка с починкой разметки и фолбэком на plain text."""
    last: Message | None = None
    chunks = render_for_telegram(reply.text).chunks or [reply.text]
    for index, chunk in enumerate(chunks):
        markup = None
        if reply.needs_confirmation and index == len(chunks) - 1 and reply.pending_id:
            markup = _confirm_keyboard(reply.pending_id, len(reply.pending))
        try:
            last = await bot.send_message(
                chat_id, chunk, reply_markup=markup, link_preview_options=None
            )
        except TelegramBadRequest:
            # разметка всё-таки не прошла (например, <a> с относительным href) — уходим в текст:
            # теги снимаем (а не экранируем), иначе владелец читает <code> вместо ответа
            last = await bot.send_message(
                chat_id,
                html_lib.escape(strip_tags(chunk), quote=False),
                reply_markup=markup,
                link_preview_options=None,
            )
        except TelegramAPIError as exc:
            log.error("telegram.send_failed", err=repr(exc)[:300], chat_id=chat_id)
            raise
    return last


# ----------------------------------------------------------------- команды


@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    await message.answer(_HELP)


@router.message(Command("new"))
async def cmd_new(message: Message, app: App) -> None:
    if message.from_user is None:
        return
    await app.supervisor.reset(message.from_user.id)
    await message.answer("Контекст диалога сброшен. Факты и заметки остались в базе.")


@router.message(Command("cost"))
async def cmd_cost(message: Message, app: App) -> None:
    snap = await app.cost.snapshot()
    bar = "▮" * min(10, int(round(snap["ratio"] * 10))) or "▯"
    await message.answer(
        f"Сегодня {snap['day']}: <code>${snap['spent_usd']:.3f}</code> из "
        f"<code>${snap['limit_usd']:.2f}</code> {bar} ({snap['ratio'] * 100:.0f}%)\n"
        f"Уровень деградации: {snap['degradation_level']} (0 — норма, 1 — без thinking, "
        f"2 — только fast-модель)"
    )


@router.message(Command("status"))
async def cmd_status(message: Message, app: App) -> None:
    if message.from_user is None:
        return
    status = await app.supervisor.status(message.from_user.id)
    models = "\n".join(
        f"• {role}: <code>{name}</code>" for role, name in status["gateway"]["models"].items()
    )
    cost: dict[str, Any] = status["cost"]
    budget_line = f"${cost['spent_usd']:.3f} из ${cost['limit_usd']:.2f}"
    trace = _trace_label(app, status)
    # ключ от соседнего сервиса — то, из-за чего «Модели недоступны» на ровном месте:
    # показываем это и в /status, потому что именно туда отправляет деградированный ответ
    auth_warn = str(status["gateway"].get("auth_hint") or "")
    lines = [
        "<b>Состояние</b>",
        f"Трассировка: {trace}",
        _repro_line(status),
        f"Промпт: <code>{status['prompt_version']}</code> · итераций ≤ {status['max_iterations']}",
        f"Модели:\n{models}",
        f"Fallback: {'включён' if status['gateway']['fallback_enabled'] else 'выключен'}",
    ]
    if auth_warn:
        lines.append(f"⚠️ {auth_warn}")
    lines += [
        f"Бюджет: <code>{budget_line}</code>",
        f"Kill switch: <b>{_kill_label(status)}</b>",
        _polish_line(status),
        _reminders_line(status),
        f"Живой ответ: {_stream_label(app.cfg)}",
        _notes_line(status),
        f"История в контексте: {status['history_messages']} реплик",
        f"Инструментов: {len(status['tools'])}",
    ]
    await message.answer("\n".join(lines))


@router.message(Command("tools"))
async def cmd_tools(message: Message, app: App) -> None:
    lines = []
    for name in app.registry.names(include_disabled=False):
        spec = app.registry.get(name)
        flag = "🔒" if spec.writes else "👁"
        risk = "" if spec.risk == "none" else f" · риск {spec.risk}"
        lines.append(f"• <code>{name}</code> {flag}{risk}")
    await message.answer("<b>Инструменты</b>\n" + "\n".join(lines))


@router.message(Command("replay"))
async def cmd_replay(message: Message, app: App, command: CommandObject) -> None:
    """Повторить ход по журналу (M1): тот же вход, замороженный мир, вердикт судьи.

    Без аргумента берём последний ход этого владельца: просить человека достать UUID из лога —
    значит сделать функцию, которой никто не воспользуется.
    """
    if message.from_user is None:
        return
    from aegis.governance.replay import replay_trace

    recorder = app.repro
    if not recorder.enabled:
        await message.answer(
            "Журнал решений не ведётся (REPRO_ENABLED=false или нет БД) — воспроизводить нечего."
        )
        return
    ref = (command.args or "").strip()
    trace_id = ref or await recorder.latest_trace(owner_id=message.from_user.id) or ""
    if not trace_id:
        await message.answer("В журнале нет ни одного хода. Напиши что-нибудь, потом /replay.")
        return
    matches = await recorder.matching_traces(trace_id)
    if not matches:
        await message.answer(
            f"Ход {trace_id[:13]} не найден: нужен UUID целиком или его начало от 6 символов."
        )
        return
    if len(matches) > 1:
        await message.answer(
            "Начало подходит к нескольким ходам: " + ", ".join(m[:13] for m in matches)
        )
        return
    pending = await message.answer("Повторяю ход: модель + судья, это два запроса…")
    report = await replay_trace(matches[0], recorder=recorder, gateway=app.gateway)
    await pending.edit_text(html_lib.escape(report.as_text(), quote=False)[:3800])


@router.message(Command("halt"))
async def cmd_halt(message: Message, app: App, command: CommandObject) -> None:
    reason = (command.args or "вручную из Telegram").strip()
    await app.kill_switch.activate(reason)
    await message.answer(
        "⏸ Записи заморожены. Бот отвечает, ищет, читает — но ничего не меняет "
        "и не подтверждает. Вернуть: <code>/resume</code>."
    )


@router.message(Command("resume"))
async def cmd_resume(message: Message, app: App) -> None:
    await app.kill_switch.release()
    await message.answer("▶ Записи разморожены.")


# ---------------------------------------------------------------- сообщения


@router.message(F.photo | F.document.mime_type.startswith("image/"))
async def on_image(message: Message, bot: Bot, app: App) -> None:
    if message.from_user is None:
        return
    file_id = (
        message.photo[-1].file_id
        if message.photo
        else (message.document.file_id if message.document else None)
    )
    if not file_id:
        await message.answer("Не разобрал, какой файл скачать.")
        return
    stored = await bot.get_file(file_id)
    if not stored.file_path:
        await message.answer("Telegram не отдал путь к файлу.")
        return
    stream = await bot.download_file(stored.file_path)
    data = stream.read() if stream is not None else b""
    if not data:
        await message.answer("Файл пришёл пустым.")
        return
    attachments = [Attachment(data=data, mime=sniff_mime(data) or "image/jpeg", kind="image")]
    text = message.caption or "Что на изображении? Извлеки тексты и цифры, скажи, зачем это."
    await run(
        message,
        app,
        Inbound(text=text, owner_id=message.from_user.id, attachments=attachments),
        bot,
    )


@router.message(F.voice | F.audio)
async def on_voice(message: Message) -> None:
    await message.answer("Голос подключается на шаге 2 (faster-whisper). Пока — текстом или фото.")


@router.message(F.sticker | F.video | F.animation)
async def on_unsupported(message: Message) -> None:
    await message.answer(
        "Это сообщение я пока не умею разбирать (видео/стикеры — шаг 4). Если нужно сохранить — "
        "опиши словами или пришли ссылкой."
    )


#: Команды, которые знает бот. aiogram требует точного совпадения имени команды, поэтому
#: «/restart» без этой проверки улетал бы в модель как обычный текст: трата токенов и
#: загадочный ответ вместо «такой команды нет».
KNOWN_COMMANDS = frozenset({"start", "help", "new", "cost", "status", "tools", "halt", "resume"})
_COMMAND_SHAPED = re.compile(r"^/([A-Za-z][A-Za-z0-9_]{1,31})(?:@\w+)?$")
#: команды, которые просят сделать что-то с самим процессом — это делается снаружи
OUTSIDE_COMMANDS = frozenset({"restart", "reboot", "stop", "update", "upgrade", "logs", "pull"})
HELP_HINT = "Список команд и их смысл — в <code>/help</code>."


def _unknown_command(text: str) -> str | None:
    """Имя неизвестной команды либо None. Путь вида /home/user/file — не команда."""
    stripped = text.strip()
    if not stripped:
        return None
    first = stripped.split(maxsplit=1)[0]
    match = _COMMAND_SHAPED.match(first)
    if not match:
        return None
    name = match.group(1).casefold()
    return None if name in KNOWN_COMMANDS else name


@router.message(F.text)
async def on_text(message: Message, app: App, bot: Bot) -> None:
    if message.from_user is None or not message.text:
        return
    unknown = _unknown_command(message.text)
    if unknown is not None:
        text = f"Не знаю команду <code>/{unknown}</code>. " + HELP_HINT
        if unknown in OUTSIDE_COMMANDS:
            text += (
                "<br>Управлением контейнером я не занимаюсь: "
                "<code>docker compose -f deploy/docker-compose.yml restart bot</code>"
            )
        await message.answer(text)
        return
    await run(message, app, Inbound(text=message.text, owner_id=message.from_user.id), bot)


@router.callback_query(F.data.regexp(r"^(ok|no):"))
async def on_confirm(callback: CallbackQuery, app: App, bot: Bot) -> None:
    if callback.from_user is None or not callback.data:
        return
    action, pending_id = callback.data.split(":", 1)
    await callback.answer("Принято" if action == "ok" else "Отменено")
    message = callback.message
    if isinstance(message, Message) and message.reply_markup is not None:
        try:
            await message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError as exc:  # сообщение могло устареть — не критично
            log.debug("confirm.markup_clear_failed", err=repr(exc)[:200])
    if not isinstance(message, Message):
        return
    reply = await app.supervisor.resume(pending_id, action == "ok", callback.from_user.id)
    await send_reply(bot, message.chat.id, reply)


async def run(message: Message, app: App, inbound: Inbound, bot: Bot | None = None) -> None:
    """Общий путь: индикатор → supervisor → отправка; ошибка — в чат и, если задан, в алерты.

    При `STREAM_REPLIES=true` «индикатор» перестаёт быть индикатором: он и есть ответ, который
    дописывается на месте. Отсюда порядок — сначала пробуем `finish` (если правки хватило, выходим),
    и только иначе удаляем черновик и идём обычной отправкой. Другой порядок дал бы либо потерянный
    ответ, либо два одинаковых сообщения.
    """
    bot = bot or message.bot
    status = await message.answer(PLACEHOLDER)
    stream = make_stream(status, app.cfg)
    try:
        reply = await app.supervisor.handle(inbound, on_delta=stream.push if stream else None)
    except Exception as exc:  # noqa: BLE001 - владельцу показываем деградацию, а не traceback
        log.exception("handle.failed")
        reply = Reply(
            # без разметки: это аварийный текст, он обязан дойти независимо от того, принял
            # Telegram HTML или нет
            text=(
                f"Сбой: {type(exc).__name__}. Команды продолжают работать — напиши /status. "
                f"Детали: {redact_secrets(str(exc))[:300]}"
            ),
            degraded=True,
        )
        await notify_failure(app, bot, exc)
    if stream is not None:
        markup = (
            _confirm_keyboard(reply.pending_id, len(reply.pending))
            if reply.needs_confirmation and reply.pending_id is not None
            else None
        )
        if await stream.finish(reply.text, markup=markup):
            return
    try:
        await status.delete()
    except TelegramAPIError:
        pass
    if bot is not None:
        await send_reply(bot, message.chat.id, reply)


def _stream_label(cfg: Any) -> str:
    """Включён ли живой ответ.

    Строка нужна, потому что «бот молчит сорок секунд» и «бот правит сообщение по кускам» для
    владельца выглядят одинаково, а различаются одной настройкой — и первый вопрос после этого
    всегда «он вообще работает?».
    """
    if not getattr(cfg, "stream_replies", False):
        return "выключен (ответ одним сообщением)"
    ms = int(getattr(cfg, "stream_edit_interval_ms", 900))
    return f"включён, правка раз в {ms} мс"


def _notes_line(status: dict[str, Any]) -> str:
    """Очередь эмбеддингов заметок.

    Строка нужна, потому что «поиск находит не то» имеет две причины с одинаковым симптомом: индекс
    отстаёт (лечится тиком) и индекс работать не должен (Null-магазин, БД без pgvector).
    """
    notes = status.get("notes") or {}
    if not notes.get("available"):
        return "Заметки: счётчик индекса недоступен"
    pending = int(notes.get("pending") or 0)
    if not pending:
        return "Заметки: эмбеддинги построены для всех"
    return f"Заметки: {pending} ждут эмбеддинга — поиск по тексту (aegis index notes)"


def _polish_line(status: dict[str, Any]) -> str:
    """Сверка с источниками и карантин внешнего текста — то, что «подкручивает» ответ.

    Строка есть, чтобы «почему мне не сказали, что число не из источника» не решалось чтением кода:
    выключенный `VERIFY_ENABLED` выглядит точно так же, как «проверили и всё хорошо».
    """
    polish = status.get("answer_polish") or {}
    verify = "вкл" if polish.get("verify") else "выкл"
    quarantine = "вкл" if polish.get("quarantine") else "выкл"
    tail = ", только при расхождении чисел" if not polish.get("always") else ""
    if not polish.get("verify"):
        tail = " — ответы не проверяются на опору в источниках"
    return f"Ответы: сверка с источниками {verify}{tail} · карантин внешнего текста {quarantine}"


def _reminders_line(status: dict[str, Any]) -> str:
    """Отдельная строка про расписание: «напоминание не пришло» бывает тремя разными случаями.

    Выключенная возможность, отсутствующая БД и живой тик с просроченной строкой выглядят для
    владельца одинаково («молчат»), поэтому различие обязано быть в /status, а не в чтении кода.
    """
    info = status.get("reminders") or {}
    if info.get("error"):
        return f"Напоминания: ⚠️ счётчик не читается — {str(info['error'])[:80]}"
    if not info.get("enabled"):
        return (
            "Напоминания: <b>некуда сохранять</b> (нет БД или REMINDERS_ENABLED=false) — "
            "инструмент отвечает отказом, а не обещанием"
        )
    scheduled = int(info.get("scheduled") or 0)
    overdue = int(info.get("overdue") or 0)
    failed = int(info.get("failed") or 0)
    bits = [f"{scheduled} в расписании"]
    if overdue:
        bits.append(f"⚠️ {overdue} пора (ждут тика)")
    if failed:
        bits.append(f"⚠️ {failed} с исчерпанными попытками")
    bits.append(f"тик ≤ {info.get('batch', 20)} за проход")
    return "Напоминания: " + ", ".join(bits)


def _repro_line(status: dict[str, Any]) -> str:
    """Отдельная строка про журнал: «аудит пишется» и «ход воспроизводим» — разные обещания.

    Аудит — строки для ``/cost``, журнал — содержимое промптов и ответов. Первое может жить без
    второго, и владелец обязан видеть, что именно у него включено: иначе ``/replay`` выглядит
    сломанным, а не выключенным.
    """
    if not status.get("repro_enabled"):
        return (
            "Журнал решений: <b>выключен</b> (REPRO_ENABLED) — /replay и «почему так ответил» "
            "не работают"
        )
    failures = int(status.get("repro_failures") or 0)
    if failures:
        return f"Журнал решений: ⚠️ {failures} сбоев записи — проверь миграции и диск"
    return "Журнал решений: ведётся (промпты, ответы, решения policy)"


def _trace_label(app: App, status: dict[str, Any]) -> str:
    """«Трассировка: ok» должна значить, что строки реально пишутся, а не что порт открыт."""
    if not app.db_ready:
        return "БД не настроена — только память"
    if status["tracing_degraded"]:
        n = int(status["tracing_failures"])
        return (
            f"⚠️ не пишется ({n} {_plural_ru(n, ('сбой', 'сбоя', 'сбоев'))}): накай миграции — "
            "<code>docker compose -f deploy/docker-compose.yml run --rm bot "
            "alembic upgrade head</code>"
        )
    return "события/аудит в БД"


def _plural_ru(n: int, forms: tuple[str, str, str]) -> str:
    """Русская плюрализация: 1 сбой / 2 сбоя / 5 сбоев / 11 сбоев."""
    if 10 <= n % 100 <= 19:  # noqa: PLR2004
        return forms[2]
    last = n % 10
    if last == 1:
        return forms[0]
    if last in (2, 3, 4):
        return forms[1]
    return forms[2]


def _kill_label(status: dict[str, Any]) -> str:
    """Строка состояния kill switch: «активен» обязан быть заметен в /status."""
    return "АКТИВЕН — записи запрещены" if status["kill_switch"]["active"] else "выключен"


async def notify_failure(app: App, bot: Bot | None, exc: BaseException) -> None:
    """Дублируем сбой в alerts-чат: днём бот ещё может ответить ошибкой, ночью — молчит."""
    chat_id = app.cfg.telegram_alerts_chat_id
    if not chat_id or bot is None:
        return
    try:
        await bot.send_message(chat_id, f"⚠️ aegis: {type(exc).__name__}: {str(exc)[:500]}")
    except TelegramAPIError as inner:  # noqa: BLE001 - алерт не перекрывает основную ошибку
        log.warning("alerts.failed", err=repr(inner)[:200])


def build_dispatcher(app: App) -> tuple[Bot, Dispatcher]:
    bot = Bot(
        app.cfg.telegram_bot_token.get_secret_value() if app.cfg.telegram_bot_token else "unset",
        default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True),
    )
    dp = Dispatcher(app=app)
    if app.cfg.telegram_owner_id is not None:
        dp.update.outer_middleware(OwnerOnly(app.cfg.telegram_owner_id))
    dp.include_router(router)
    return bot, dp


async def main() -> None:
    from aegis.agents.tools.registry import registry

    try:
        app = build_app(registry=registry)
        app.cfg.require_runtime()
    except ConfigError as exc:
        # это самый частый «бот не стартует»: конфиг. Трейсбек тут только мешает
        print(f"! конфигурация неполная: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    bot, dp = build_dispatcher(app)
    log.info("bot.start", owner_id=app.cfg.telegram_owner_id, tools=len(app.registry.names()))
    if app.db_ready:
        # Старт не блокируем: без схемы бот полезен, но оператор должен узнать сразу, а не по
        # «Сбой: ...» в каждом ответе (connect-ok != schema-ok).
        await app.probe_schema()
    try:
        async with bot:
            await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    except Exception as exc:  # noqa: BLE001 - стартовый сбой сети/токена: нужен диагноз
        name = type(exc).__name__
        hints = {
            "TelegramUnauthorized": "токен неверный или отозван — сверь TELEGRAM_BOT_TOKEN",
            "TelegramNetworkError": "нет связи с api.telegram.org: проверь прокси/файрвол и то, "
            "что TLS не перехватывается (песочницы и корпоративный прокси так умеют)",
            "TelegramServerError": "Telegram 5xx — обычно проходит само, рестарт через минуту",
        }
        hint = hints.get(name, "стартовое обращение к Telegram не удалось")
        print(f"! {name}: {hint}", file=sys.stderr)
        raise SystemExit(3) from exc
    finally:
        await app.aclose()


if __name__ == "__main__":
    asyncio.run(main())
