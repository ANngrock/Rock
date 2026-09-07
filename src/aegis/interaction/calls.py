"""Звонок как канал доставки напоминаний: один HTTP-вызов — и владелец услышал.

Ровно два провайдера, и оба — без нашей инфраструктуры приёма вызовов:

* **Twilio REST** — TwiML с ``<Say>`` передаётся *телом* запроса, публичный URL не нужен. Это
  принципиально: «колбэк на бота» означал бы открыть входную дверь наружу ради одной функции.
* **generic webhook** — ``POST {"to","text"}`` на свой шлюз (Asterisk/FreePBX/софтфон-мост) для
  тех, кто телефонию уже хостит у себя.

Провайдер намеренно тупой: получил текст — позвонил — сказал «да/нет». «Что говорить» собирается
выше (``interaction.notify``), иначе проверка произнесённой фразы расплющилась бы по двум модулям.

Ответ провайдера — «вызов принят» (queued/ringing): ждать окончания разговора нельзя, тик
завис бы на чужом «алло» минут на десять; честная цена такого решения — «дозвонились» означает
«провайдер принял вызов», и только.
"""

from __future__ import annotations

from typing import Any, Protocol
from xml.sax.saxutils import escape

import httpx
import structlog

from aegis.platform.config import Settings

__all__ = ["CallError", "CallProvider", "TwilioCall", "WebhookCall", "call_provider_from_settings"]

log = structlog.get_logger(__name__)

#: потолок символов в Say: Twilio режет длиннее молча, а «обрезанное напоминание» — это тот же
#: потерянный хвост, что и «недочитанное письмо»; лучше явное усечение, чем тайное
MAX_SPOKEN_CHARS = 600


class CallError(RuntimeError):
    """Провайдер отказал (или не ответил) — диспетчер решает, что делать с этим."""


class CallProvider(Protocol):
    async def call(self, to: str, text: str) -> None: ...


def _spoken_body(text: str) -> str:
    """Одна строка, без разметки и управляльного шума:Say читает то, что видит."""
    return " ".join(str(text).split())[:MAX_SPOKEN_CHARS]


class TwilioCall:
    """Программный звонок через ``Calls.json``: To/From + инлайновый TwiML.

    Язык ``ru-RU`` проставлен явно: по умолчанию Say произносит кириллицу голосом en-US, и вместо
    «встреча в пятнадцать ноль ноль» владелец слушает кашу — «напоминание не дошло» в худшем виде:
    формально дошло.
    """

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        from_number: str,
        timeout_s: int = 12,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._sid = account_sid
        self._token = auth_token
        self._from = from_number
        self._timeout = timeout_s
        self._transport = transport

    async def call(self, to: str, text: str) -> None:
        twiml = f'<Response><Say language="ru-RU">{escape(_spoken_body(text))}</Say></Response>'
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self._sid}/Calls.json"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(
                    url,
                    data={"To": to, "From": self._from, "Twiml": twiml},
                    auth=(self._sid, self._token),
                )
        except httpx.HTTPError as exc:
            raise CallError(f"twilio: {type(exc).__name__}: {str(exc)[:200]}") from exc
        if resp.status_code >= 400:
            raise CallError(f"twilio HTTP {resp.status_code}: {resp.text[:180]}")
        log.info("call.accepted", provider="twilio", to=_mask(to))

    def describe(self) -> str:  # pragma: no cover — только для диагностики CLI
        return f"twilio · from {self._from}"


class WebhookCall:
    """POST {"to","text"} на свой шлюз. Тело намеренно скучное — его пишут на любой стороне."""

    def __init__(
        self, *, url: str, timeout_s: int = 12, transport: httpx.AsyncBaseTransport | None = None
    ):
        self._url = url
        self._timeout = timeout_s
        self._transport = transport

    async def call(self, to: str, text: str) -> None:
        payload = {"to": to, "text": _spoken_body(text), "language": "ru-RU"}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise CallError(f"webhook: {type(exc).__name__}: {str(exc)[:200]}") from exc
        if resp.status_code >= 400:
            raise CallError(f"webhook HTTP {resp.status_code}: {resp.text[:180]}")
        log.info("call.accepted", provider="webhook", to=_mask(to))

    def describe(self) -> str:  # pragma: no cover
        return f"webhook · {self._url[:80]}"


def _mask(to: str) -> str:
    """Номер в лог — только последние четыре цифры: лог читается не только владельцем."""
    digits = "".join(c for c in str(to) if c.isdigit())
    return f"+…{digits[-4:]}" if len(digits) > 4 else "…"


def call_provider_from_settings(cfg: Settings) -> Any | None:
    """None = канал не настроен — это штатный режим «доставка сообщением», а не поломка.

    Недообъявленный провайдер (twilio без ключей) молча деградирует сюда же: в момент
    ``aegis bot`` это уже не пропустит ``require_runtime`` (ConfigError с именами ключей), а
    «поздно включённый» тик не имеет права падать — напоминание должно дойти хоть как-то.
    """
    provider = (cfg.call_provider or "none").strip().lower()
    if provider == "twilio" and cfg.twilio_account_sid and cfg.twilio_auth_token:
        return TwilioCall(
            account_sid=cfg.twilio_account_sid,
            auth_token=cfg.twilio_auth_token.get_secret_value(),
            from_number=cfg.twilio_from_number,
            timeout_s=cfg.call_timeout_seconds,
        )
    if provider == "webhook" and (cfg.call_webhook_url or "").startswith("http"):
        return WebhookCall(url=cfg.call_webhook_url, timeout_s=cfg.call_timeout_seconds)
    return None
