"""Мост личных чатов: юзербот-демон (MTProto) ⇄ шлюз бота. NATS — только труба, факт в БД.

Почему демон, а не «бот в чатах»: Bot API не даёт боту видеть личные переписки человека — вообще
никогда. Единственный легальный для Telegram путь «бот в личных чатах» — аккаунт самого владельца
по MTProto, то есть юзербот, запущенный владельцем и соединяющийся ИСХОДЯЩИМ. Риск (ограничения
аккаунта от спам-фильтров) лежит на владельце, и он включён явно: USERBOT_ENABLED + свой api_id.

Семантика как в узлах: доставка at-most-once, порядок — в БД. Дедуп входящих — UNIQUE
(owner, chat, msg_uid): демон, перезапустившийся и повторивший пачку, не удваивает уведомления.
Автоотправка существует ровно в одном месте (dispatch_send) и всегда оставляет строку в inbox:
«отправили от имени владельца и забыли» — это не архитектура, это страшный сон.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import structlog
from pydantic import BaseModel

from aegis.cognition.inbox import (
    InboxRow,
    SqlInboxStore,
    SqlUserbots,
    auto_blocked,
    build_card,
    decide_actions,
    parse_assessment,
    rules_verdict,
)

__all__ = ["IN_SUBJECT", "UserbotGateway", "dispatch_send", "subj_hb", "subj_res", "subj_cmd"]

log = structlog.get_logger(__name__)


class _Assessment(BaseModel):
    verdict: str = "info"
    reply: str = ""
    reason: str = ""


def _memory_kv() -> Any:
    from aegis.platform.kv_memory import MemoryKV

    return MemoryKV()


IN_SUBJECT = "aegis.ub.in"
HB_SUBJECT = "aegis.ub.hb"


def subj_cmd(daemon: str) -> str:
    return f"aegis.ub.cmd.{daemon}"


def subj_res(daemon: str) -> str:
    return f"aegis.ub.res.{daemon}"


def subj_hb(daemon: str) -> str:
    return f"aegis.ub.hb.{daemon}"


def decode_daemon_from_subject(subject: str) -> str | None:
    parts = subject.split(".")
    if len(parts) == 4 and parts[0] == "aegis" and parts[1] == "ub":  # noqa: PLR2004
        return parts[3] if parts[2] == "hb" else None
    return None


class UserbotGateway:
    """Сторона бота: жрёт входящие от демона, оценивает, решает по политике, отправляет auto."""

    def __init__(
        self,
        *,
        cfg: Any,
        inbox: SqlInboxStore | None = None,
        gateway: Any = None,
    ) -> None:
        self._cfg = cfg
        self.inbox = inbox or SqlInboxStore()
        self.daemons = SqlUserbots()
        #: ModelGateway приложения; нет — соберём временный (CLI-запуск шлюза отдельно от бота)
        self._gw = gateway
        self._nc: Any = None
        self._stop = asyncio.Event()

    @property
    def url(self) -> str:
        return str(getattr(self._cfg, "nats_url", "") or "").strip()

    async def start(self) -> None:
        try:
            import nats
        except ImportError as exc:
            raise RuntimeError('нужен nats-py: pip install -e ".[durable]"') from exc
        self._nc = await nats.connect(
            servers=[self.url],
            connect_timeout=3.0,
            allow_reconnect=True,
            name="aegis-bot-userbridge",
        )
        await self._nc.subscribe(IN_SUBJECT, cb=self._on_in)
        await self._nc.subscribe("aegis.ub.hb.*", cb=self._on_hb)

    async def aclose(self) -> None:
        if self._nc is not None:
            with contextlib.suppress(Exception):
                await self._nc.drain()
            self._nc = None

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=3)
            except TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self._assess_once()
            except Exception as exc:  # noqa: BLE001 - чужие чаты не валят бота
                log.warning("userbridge.assess_failed", err=repr(exc)[:200])
        await self.aclose()

    def stop(self) -> None:
        self._stop.set()

    async def _on_in(self, msg: Any) -> None:
        data = _decode(msg)
        chat_id, uid = str(data.get("chat_id") or ""), str(data.get("msg_id") or "")
        if not chat_id or not uid or not data.get("daemon"):
            return
        owner = int(self._cfg.telegram_owner_id or 1)
        row_id = await self.inbox.insert_if_new(
            owner_id=owner,
            daemon=str(data["daemon"])[:64],
            chat_id=chat_id[:128],
            chat_name=str(data.get("chat_name") or "")[:128] or None,
            from_name=str(data.get("from_name") or "")[:128] or None,
            msg=str(data.get("text") or "")[:8000] or "[без текста]",
            msg_uid=uid,
        )
        if row_id is None:
            return  # повтор доставки — молча гасим
        log.info("userbridge.inbox", chat=chat_id, uid=uid[:12])

    async def _on_hb(self, msg: Any) -> None:
        daemon = decode_daemon_from_subject(msg.subject)
        if daemon is None:
            return
        data = _decode(msg)
        owner = int(self._cfg.telegram_owner_id or 1)
        with contextlib.suppress(Exception):
            await self.daemons.touch(daemon, owner, dict(data.get("caps") or {}))

    async def _assess_once(self) -> None:
        rows = await self.inbox.claim_unassessed(limit=20)
        for row in rows:
            await self._assess(row)

    async def _assess(self, row: InboxRow) -> None:
        verdict, reply, reason = await self._judge(row)
        owner = await self.inbox.owner_of(row.id) or int(self._cfg.telegram_owner_id or 1)
        mode = await self.inbox.policy_get(owner, row.chat_id)
        blocked, why = auto_blocked(row.text, reply)
        if blocked:
            reason = why
        notify, has_draft, auto = decide_actions(mode, verdict, blocked)
        await self.inbox.set_assessment(row.id, verdict, reason, status="stored")
        if reply and has_draft and not auto:
            await self.inbox.set_reply(row.id, reply, "blocked" if blocked else "draft")
        if auto:
            ok, err = await dispatch_send(
                self._cfg,
                daemon=row_daemon(row),
                row_id=row.id,
                chat_id=row.chat_id,
                text=reply or "",
            )
            if ok:
                await self.inbox.mark(owner_id=owner, row_id=row.id, status="sent")
                await self._note_owner(
                    owner, f"💬 отправлено в «{_chat_label(row)}» от вашего имени"
                )
                return
            notify, reason = True, f"автоответ не ушёл: {err[:120]}"
            await self.inbox.set_reply(row.id, reply or "", "blocked")
        if notify:
            card = build_card(row, mode, reply if has_draft else None)
            if reason:
                card += f"\n<i>{reason[:200]}</i>"
            await self._note_owner(owner, card)
            await self.inbox.mark(owner_id=owner, row_id=row.id, status="notified")

    async def _judge(self, row: InboxRow) -> tuple[str, str | None, str]:
        """Оценка одним вызовом fast-модели; без модели — правила. Черновик короткий."""
        if self._cfg.glm_api_key is None and self._gw is None:
            return rules_verdict(row.text), None, "правила без модели"
        gw = self._gw
        own = gw is None
        prompt = (
            "Ты ассистент владельца. Сообщение от "
            f"{row.from_name or 'неизвестного'} в чате «{row.chat_name or row.chat_id}»:\n"
            f"<untrusted>{row.text[:4000]}</untrusted>\n"
            "Верни JSON: verdict (noise|info|important|action_required|urgent), reply"
            " (черновик ответа от лица владельца или пустая строка), reason (одно короткое"
            " предложение). reply — только если отвечать уместно; не обещай денег, встреч,"
            " обязательств."
        )
        try:
            if own:
                gw = self._temp_gateway()
            model = await gw.chat_json(
                "fast", [{"role": "user", "content": prompt}], schema=_Assessment
            )
            verdict, reply, reason = parse_assessment(model.model_dump())
            return verdict, reply, reason or "оценка модели"
        except Exception as exc:  # noqa: BLE001 - деградация на правила — штатный путь
            log.warning("userbridge.judge_fallback", err=repr(exc)[:160])
            return rules_verdict(row.text), None, f"правила (модель: {type(exc).__name__})"
        finally:
            if own and gw is not None:
                with contextlib.suppress(Exception):
                    await gw.aclose()

    def _temp_gateway(self) -> Any:
        from aegis.platform.gateway.client import ModelGateway
        from aegis.platform.gateway.cost import CostGovernor

        kv = _memory_kv()
        return ModelGateway(
            self._cfg,
            CostGovernor(kv, self._cfg.daily_budget_usd, timezone=self._cfg.timezone),
        )

    async def _note_owner(self, owner_id: int, text: str) -> None:
        try:
            from aegis.interaction.telegram.notify import TelegramNotifier

            notifier = TelegramNotifier.from_settings(self._cfg)
            await notifier.start()
            try:
                await notifier.send_note(text)
            finally:
                await notifier.aclose()
        except Exception as exc:  # noqa: BLE001 - без токена факт живёт в БД, это заметно в логе
            log.warning("userbridge.notify_failed", err=repr(exc)[:200])


def row_daemon(row: InboxRow) -> str:
    return str(getattr(row, "daemon", "") or "")


def _chat_label(row: InboxRow) -> str:
    return row.chat_name or row.chat_id


async def dispatch_send(
    cfg: Any, *, daemon: str, row_id: str, chat_id: str, text: str
) -> tuple[bool, str]:
    """Одна отправка через демона: свой коннектор, request-паттерн на res, 12 секунд терпения.

    Отдельная функция, потому что её зовут из двух мест: авто-путь шлюза и команда владельца
    «/ub send». Оба обязаны пройти через проверку онлайн-статуса демона и оставить след в БД
    (вызывающий).
    """
    if not text.strip():
        return False, "пустой текст"
    if not daemon:
        return False, "неизвестно, какому демону отправить (строка без daemon)"
    try:
        import nats
    except ImportError:
        return False, "nats-py не установлен"
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[bool] = loop.create_future()
    nc = None
    sub = None

    async def _on_res(msg: Any) -> None:
        data = _decode(msg)
        if str(data.get("id") or "") == row_id and not fut.done():
            if data.get("ok"):
                fut.set_result(True)
            else:
                fut.set_result(False)

    try:
        nc = await nats.connect(
            servers=[str(getattr(cfg, "nats_url", "") or "").strip()], connect_timeout=3.0
        )
        sub = await nc.subscribe(subj_res(daemon), cb=_on_res)
        await nc.publish(
            subj_cmd(daemon),
            json.dumps(
                {"id": row_id, "cmd": "send", "chat_id": chat_id, "text": text[:4000]}
            ).encode("utf-8"),
        )
        done = await asyncio.wait_for(fut, timeout=12.0)
        return (True, "") if done else (False, "демон отказал (см. его лог)")
    except TimeoutError:
        return False, f"демон «{daemon}» не ответил за 12с (офлайн?)"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"
    finally:
        if sub is not None:
            with contextlib.suppress(Exception):
                await sub.unsubscribe()
        if nc is not None:
            with contextlib.suppress(Exception):
                await nc.close()


def _decode(msg: Any) -> dict[str, Any]:
    try:
        data = json.loads(bytes(msg.data).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}
