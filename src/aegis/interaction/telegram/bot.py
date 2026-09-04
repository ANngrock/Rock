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
from aegis.agents.tools import builtin  # noqa: F401  — импорт регистрирует инструменты
from aegis.agents.tools.images import sniff_mime
from aegis.agents.tools.registry import Attachment
from aegis.interaction.telegram.render import render_for_telegram
from aegis.platform.config import ConfigError
from aegis.runtime import App, build_app

__all__ = ["OwnerOnly", "build_dispatcher", "main"]

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
            # разметка всё-таки не прошла (например, <a> с относительным href) — уходим в текст
            last = await bot.send_message(
                chat_id, html_lib.escape(chunk), reply_markup=markup, link_preview_options=None
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
    await message.answer(
        "<b>Состояние</b>\n"
        f"Трассировка: {trace}\n"
        f"Промпт: <code>{status['prompt_version']}</code> · итераций ≤ {status['max_iterations']}\n"
        f"Модели:\n{models}\n"
        f"Fallback: {'включён' if status['gateway']['fallback_enabled'] else 'выключен'}\n"
        f"Бюджет: <code>{budget_line}</code>\n"
        f"Kill switch: <b>{_kill_label(status)}</b>\n"
        f"История в контексте: {status['history_messages']} реплик\n"
        f"Инструментов: {len(status['tools'])}"
    )


@router.message(Command("tools"))
async def cmd_tools(message: Message, app: App) -> None:
    lines = []
    for name in app.registry.names(include_disabled=False):
        spec = app.registry.get(name)
        flag = "🔒" if spec.writes else "👁"
        risk = "" if spec.risk == "none" else f" · риск {spec.risk}"
        lines.append(f"• <code>{name}</code> {flag}{risk}")
    await message.answer("<b>Инструменты</b>\n" + "\n".join(lines))


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


@router.message(F.text)
async def on_text(message: Message, app: App, bot: Bot) -> None:
    if message.from_user is None or not message.text:
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
    """Общий путь: индикатор → supervisor → отправка; ошибка — в чат и, если задан, в алерты."""
    bot = bot or message.bot
    status = await message.answer("…")
    try:
        reply = await app.supervisor.handle(inbound)
    except Exception as exc:  # noqa: BLE001 - владельцу показываем деградацию, а не traceback
        log.exception("handle.failed")
        reply = Reply(
            text=(
                f"Сбой: <code>{type(exc).__name__}</code>. Команды продолжают работать — "
                "напиши <code>/status</code>."
            ),
            degraded=True,
        )
        await notify_failure(app, bot, exc)
    try:
        await status.delete()
    except TelegramAPIError:
        pass
    if bot is not None:
        await send_reply(bot, message.chat.id, reply)


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
