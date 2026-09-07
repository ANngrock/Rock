"""Инбокс личных чатов: вердикт воронки, охрана автоответа, жизнь черновика.

Режимы на чат (`chat_policies.mode`):

* ``watch`` — только читать и хранить; владельцу летит уведомление, если важность перевалила;
* ``draft`` — бот готовит ответ и показывает его владельцу («отправить?») — авто-режим по
  умолчанию именно такой, потому что отвечать от имени человека без спроса — дерзость;
* ``auto`` — отправка без подтверждения, но только то, что прошла охрану.

Охрана (`auto_blocked`) — часть, где я перестраховщик: деньги, реквизиты, «код из СМС», юридическое,
здоровье, агрессия — всё это — handoff владельцу, какой бы режим ни стоял. Цена ложного отказа —
одно лишнее уведомление; цена ложного «оплачу, конечно» от имени владельца — жизнь.

Сама отправка из этого модуля не делает ничего: здесь решения и хранение, труба — в relay.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text as _text

__all__ = [
    "VERDICTS",
    "InboxRow",
    "SqlInboxStore",
    "SqlUserbots",
    "auto_blocked",
    "build_card",
    "decide_actions",
    "normalize_peer",
    "rules_verdict",
]

VERDICTS = ("new", "noise", "info", "important", "action_required", "urgent")
_ACT = ("action_required", "urgent")
_MODES = ("off", "watch", "draft", "auto")
_DAEMON_STALE = 180.0  # секунд: демон без heartbeat считается офлайн — черновик не улетит


def normalize_peer(peer: str) -> str:
    p = (peer or "").strip().lower()
    if not 1 <= len(p) <= 128:  # noqa: PLR2004 — как CHECK в миграции
        raise ValueError("peer — id чата или @имя, 1..128 символов")
    return p


@dataclass(frozen=True, slots=True)
class InboxRow:
    id: str
    chat_id: str
    chat_name: str
    from_name: str
    text: str
    verdict: str
    status: str
    reply: str | None
    reason: str | None
    created_at: datetime
    daemon: str = ""


_MONEY = re.compile(
    r"(перевед|перешл(и|ю|ь)|оплат|заплат|скинь|кинь\s+(деньг|на\s+карт)|реквизит|iban"
    r"|номер\s+карт|счёт\s+на\b|выпис\w*\s+сч(ёт|ет)|оплат\w*\s+сч(ёт|ет)|крипт(о|ей)|usdt|биткоин)",
    re.I,
)
_CREDENTIALS = re.compile(r"(код\s+из\s+(смс|sms)|одноразов\w+ код|парол|cvv|cvc)", re.I)
_LEGAL = re.compile(r"(полиц|суд\w*|претензи|иск\b|адвокат|жалоб|прокуратур)", re.I)
_HEALTH = re.compile(r"(скор(ая|ой)|вызыва(ю|ем) врач|температур|кровь|не дыш)", re.I)
_ANGER = re.compile(r"(иди\s+на\w*|дурак|коз(ёл|ел)|уволю|жених\w*\s*—|наглы|нагл\w+|😡|🤬)")
_PROMISE = re.compile(
    r"(оплач(у|ю)|перевед(у|ю)|заплач(у|ю)|отправл(ю|яю)\s+(деньг|перевод)|беру\s+на\s+себя"
    r"|гарант\w+|подписал(а)?\s+договор|согласен\s+на\s+[\d$])",
    re.I,
)


def auto_blocked(msg: str, reply: str | None) -> tuple[bool, str]:
    """(заблокировать?, причина для владельца). Проверены и входящие, и исходящий текст:
    мануляция «скажи что заплатишь» приходит как входящее, а исполняется нашим исходящим."""
    for rx, label in (
        (_MONEY, "деньги/оплаты"),
        (_CREDENTIALS, "коды и доступы"),
        (_LEGAL, "юридическое"),
        (_HEALTH, "здоровье"),
    ):
        if rx.search(msg or ""):
            return True, f"входящее про {label} — автоответ запрещён, нужен человек"
    if reply and _PROMISE.search(reply):
        return True, "черновик обещает действие с деньгами/обязательствами — только вручную"
    if _ANGER.search(msg or ""):
        return True, "на эмоциях — автоматические ответы тут калечат отношения"
    return False, ""


_RULES_URGENT = ("срочно", "немедленно", "аврал", "горим", "🆘")
_RULES_ACTION = ("?", "пожалу", "можешь", "сможешь", "нужен", "нужна", "надо", "подтверд")
_TAIL_PUNCT = re.compile(r"[\s.!?,)]+$")
_NOISE = frozenset(
    {"ок", "окей", "ага", "угу", "ясно", "понятно", "норм", "хорошо", "спс", "спасибо", "+", "👍"}
)


def rules_verdict(msg: str) -> str:
    """Вердикт без LLM — деградация, а не отказ: вопросы/просьбы important, остальное info."""
    low = (msg or "").lower()
    if any(w in low for w in _RULES_URGENT):
        return "urgent"
    if any(w in low for w in _RULES_ACTION):
        return "action_required"
    tokens = _TAIL_PUNCT.sub("", low).split()
    if tokens and len(low) <= 16 and all(t in _NOISE for t in tokens):  # noqa: PLR2004
        return "noise"
    return "info"


def parse_assessment(raw: dict[str, Any]) -> tuple[str, str | None, str]:
    """JSON от fast-модели → (verdict, черновик, причина) с отсечкой по алфавиту состояний."""
    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS[1:]:
        verdict = "info"
    reply = str(raw.get("reply") or "").strip()[:1500] or None  # noqa: PLR2004 — лимит сообщения с запасом
    reason = str(raw.get("reason") or "").strip()[:200] or "оценка модели"
    return verdict, reply, reason


def decide_actions(mode: str | None, verdict: str, blocked: bool) -> tuple[bool, bool, bool]:
    """(уведомить?, есть черновик?, отправить сразу?). None-политика — молчаливое хранение:
    чат, который владелец не включён, не имеет права ни на черновики, ни на отправку."""
    if mode in (None, "", "off"):
        return False, False, False
    if verdict == "noise":
        return False, False, False
    important = verdict in _ACT or verdict == "urgent" or verdict == "important"
    if mode == "watch":
        return important, False, False
    if mode == "draft":
        return important, important, False
    # auto: всё важное получает черновик; срочное и заблокированное — всегда уведомление
    if verdict == "urgent":
        return True, True, False
    if not important:
        return False, False, False
    if blocked:
        return True, True, False
    return False, True, True


_CARD_VERDICT = {
    "noise": "",
    "info": "· к сведению",
    "important": "· ⚠ важно",
    "action_required": "· ✍ нужен ответ",
    "urgent": "· ‼ срочно",
    "new": "",
}


def build_card(row: InboxRow, mode: str | None, draft: str | None) -> str:
    """HTML карточка владельцу. Цитата обрезана — не «пересказ простыни», а «что там»."""
    who = f" от <i>{_esc(row.from_name)}</i>" if row.from_name else ""
    quote = _esc(row.text[:380]) + ("…" if len(row.text) > 380 else "")  # noqa: PLR2004
    chip = _CARD_VERDICT.get(row.verdict, "")
    head = f"💬 <b>{_esc(row.chat_name or row.chat_id)}</b>{who}{chip}"
    if row.reason:
        head += f"\n<i>{_esc(row.reason[:160])}</i>"
    body = f"<blockquote>{quote}</blockquote>"
    if row.status == "draft" and draft:
        body += f"\n✍ черновик: <i>{_esc(draft[:900])}</i>"
        body += "\n«Отправить» ниже — или напиши свой текст, отправлю его."
    elif mode == "auto" and row.verdict in _ACT:
        body += "\n(режим auto, но автоответ запрещён охраной — реши сам)"
    return head + "\n" + body


def _esc(s: str) -> str:
    import html

    return html.escape(s or "", quote=False)


class SqlInboxStore:
    def __init__(self, *, session_factory: Any = None) -> None:
        self._sm = session_factory

    def _session(self) -> Any:
        from aegis.platform.db import session

        return self._sm() if self._sm is not None else session()

    async def insert_if_new(
        self,
        *,
        owner_id: int,
        daemon: str,
        chat_id: str,
        chat_name: str | None,
        from_name: str | None,
        msg: str,
        msg_uid: str,
    ) -> str | None:
        """UNIQUE(owner,chat,msg_uid) — повторная доставка демона не рождает дубль уведомления."""
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        _text(
                            "INSERT INTO cognition.inbox (owner_id, daemon, chat_id, chat_name,"
                            " from_name, text, msg_uid) VALUES (:o, :d, :c, :n, :f, :t, :u)"
                            " ON CONFLICT (owner_id, chat_id, msg_uid) DO NOTHING"
                            " RETURNING id::text AS id"
                        ),
                        {
                            "o": int(owner_id),
                            "d": daemon[:64],
                            "c": str(chat_id)[:128],
                            "n": (chat_name or "")[:128] or None,
                            "f": (from_name or "")[:128] or None,
                            "t": (msg or "")[:8000],
                            "u": str(msg_uid)[:64],
                        },
                    )
                )
                .mappings()
                .first()
            )
            await s.commit()
        return None if row is None else str(row["id"])

    async def claim_unassessed(self, *, limit: int = 20) -> list[InboxRow]:
        q = (
            "SELECT id::text AS id, owner_id, chat_id, chat_name, from_name, text, daemon,"
            " verdict, status, reply, reason, created_at FROM cognition.inbox"
            " WHERE verdict = 'new'"
            " ORDER BY created_at LIMIT :l"
        )
        async with self._session() as s:
            rows = (await s.execute(_text(q), {"l": int(limit)})).mappings().all()
        return [_row(r) for r in rows]

    async def owner_of(self, row_id: str) -> int | None:
        async with self._session() as s:
            v = await s.execute(
                _text("SELECT owner_id FROM cognition.inbox WHERE id = CAST(:i AS uuid)"),
                {"i": row_id},
            )
        got = v.scalar()
        return None if got is None else int(got)

    async def set_assessment(
        self, row_id: str, verdict: str, reason: str, status: str = "stored"
    ) -> None:
        if verdict not in VERDICTS:
            raise ValueError(f"verdict обязан быть одним из {VERDICTS}")
        async with self._session() as s:
            await s.execute(
                _text(
                    "UPDATE cognition.inbox SET verdict = :v, reason = :r, status = :st,"
                    " updated_at = now() WHERE id = CAST(:i AS uuid) AND verdict = 'new'"
                ),
                {"v": verdict, "r": reason[:200], "st": status, "i": row_id},
            )
            await s.commit()

    async def set_reply(self, row_id: str, reply: str, status: str) -> None:
        if status not in ("draft", "blocked", "sent"):
            raise ValueError("после черновика бывает draft|blocked|sent")
        async with self._session() as s:
            await s.execute(
                _text(
                    "UPDATE cognition.inbox SET reply = :t, status = :st, updated_at = now()"
                    " WHERE id = CAST(:i AS uuid)"
                ),
                {"t": reply[:1500], "st": status, "i": row_id},
            )
            await s.commit()

    async def mark(self, *, owner_id: int, row_id: str, status: str) -> bool:
        async with self._session() as s:
            updated = (
                await s.execute(
                    _text(
                        "UPDATE cognition.inbox SET status = :st, updated_at = now()"
                        " WHERE id = CAST(:i AS uuid) AND owner_id = :o"
                        " AND status IN ('stored', 'notified', 'draft')"
                    ),
                    {"st": status, "i": row_id, "o": int(owner_id)},
                )
            ).rowcount
            await s.commit()
        return bool(updated)

    async def get_by_ref(self, *, owner_id: int, ref: str) -> InboxRow | None:
        """id или его начало — как с узлами: 8 символов из карточки достаточно."""
        q = (
            "SELECT id::text AS id, chat_id, chat_name, from_name, text, daemon, verdict, status,"
            " reply, reason, created_at FROM cognition.inbox"
            " WHERE owner_id = :o AND id::text LIKE :r || '%' ORDER BY created_at DESC LIMIT 1"
        )
        async with self._session() as s:
            row = (
                (await s.execute(_text(q), {"o": int(owner_id), "r": ref[:36]})).mappings().first()
            )
        return None if row is None else _row(row)

    async def list_recent(self, *, owner_id: int, limit: int = 10) -> list[InboxRow]:
        q = (
            "SELECT id::text AS id, chat_id, chat_name, from_name, text, verdict, status,"
            " reply, reason, created_at FROM cognition.inbox WHERE owner_id = :o"
            " ORDER BY created_at DESC LIMIT :l"
        )
        async with self._session() as s:
            rows = (
                (await s.execute(_text(q), {"o": int(owner_id), "l": int(limit)})).mappings().all()
            )
        return [_row(r) for r in rows]

    # --- политики чатов ---

    async def policy_set(
        self, owner_id: int, peer: str, mode: str, note: str | None = None
    ) -> None:
        if mode not in _MODES:
            raise ValueError(f"режим — один из {_MODES}")
        async with self._session() as s:
            await s.execute(
                _text(
                    "INSERT INTO cognition.chat_policies (owner_id, peer, mode, note, updated_at)"
                    " VALUES (:o, :p, :m, :n, now()) ON CONFLICT (owner_id, peer) DO UPDATE SET"
                    " mode = EXCLUDED.mode, note = EXCLUDED.note, updated_at = now()"
                ),
                {
                    "o": int(owner_id),
                    "p": normalize_peer(peer),
                    "m": mode,
                    "n": (note or "")[:200] or None,
                },
            )
            await s.commit()

    async def policy_get(self, owner_id: int, peer: str) -> str | None:
        async with self._session() as s:
            v = await s.execute(
                _text("SELECT mode FROM cognition.chat_policies WHERE owner_id = :o AND peer = :p"),
                {"o": int(owner_id), "p": normalize_peer(peer)},
            )
        got = v.scalar()
        return None if got is None else str(got)

    async def policy_list(self, owner_id: int) -> list[dict[str, Any]]:
        async with self._session() as s:
            rows = (
                (
                    await s.execute(
                        _text(
                            "SELECT peer, mode, note FROM cognition.chat_policies"
                            " WHERE owner_id = :o ORDER BY peer"
                        ),
                        {"o": int(owner_id)},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    async def policy_remove(self, owner_id: int, peer: str) -> bool:
        async with self._session() as s:
            updated = (
                await s.execute(
                    _text("DELETE FROM cognition.chat_policies WHERE owner_id = :o AND peer = :p"),
                    {"o": int(owner_id), "p": normalize_peer(peer)},
                )
            ).rowcount
            await s.commit()
        return bool(updated)


class SqlUserbots:
    """Heartbeat демонов — та же связка, что у узлов: «онлайн» = свежий hb, не отдельная правда."""

    def __init__(self, *, session_factory: Any = None) -> None:
        self._sm = session_factory

    def _session(self) -> Any:
        from aegis.platform.db import session

        return self._sm() if self._sm is not None else session()

    async def touch(self, daemon: str, owner_id: int, caps: dict[str, Any]) -> None:
        import json

        async with self._session() as s:
            await s.execute(
                _text(
                    "INSERT INTO cognition.userbots (daemon, owner_id, last_seen, caps)"
                    " VALUES (:d, :o, now(), CAST(:c AS jsonb))"
                    " ON CONFLICT (daemon) DO UPDATE SET last_seen = now(), owner_id = :o,"
                    " caps = CAST(:c AS jsonb)"
                ),
                {"d": daemon[:64], "o": int(owner_id), "c": json.dumps(caps, default=str)[:2000]},
            )
            await s.commit()

    async def daemons(self, owner_id: int) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        async with self._session() as s:
            rows = (
                (
                    await s.execute(
                        _text(
                            "SELECT daemon, last_seen, caps::text AS caps FROM cognition.userbots"
                            " WHERE owner_id = :o ORDER BY daemon"
                        ),
                        {"o": int(owner_id)},
                    )
                )
                .mappings()
                .all()
            )
        out = []
        for r in rows:
            seen: datetime = r["last_seen"]
            age = (now - seen.astimezone(UTC)).total_seconds()
            out.append(
                {"daemon": r["daemon"], "age_s": round(age, 1), "online": age < _DAEMON_STALE}
            )
        return out


def _row(r: Any) -> InboxRow:
    return InboxRow(
        id=str(r["id"]),
        daemon=str(r.get("daemon") or ""),
        chat_id=str(r["chat_id"]),
        chat_name=str(r["chat_name"] or ""),
        from_name=str(r["from_name"] or ""),
        text=str(r["text"]),
        verdict=str(r["verdict"]),
        status=str(r["status"]),
        reply=r["reply"],
        reason=r["reason"],
        created_at=r["created_at"],
    )
