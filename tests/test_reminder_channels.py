"""Каналы доставки: разбор «за N до», провайдеры звонка, честность диспетчера.

БД здесь не нужна — и не должна: это ровно тот слой, который можно испортить молча.
Проверяется: парсер не теряет «заранее» и не принимает цепочки; провайдеры шлют то, что
обещано (TwiML экранирован, webhook — строго {to,text,language}); диспетчер при отказе
звонка доставляет текст и оставляет строку в отчёте — ни одного молчаливого fallback.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import httpx
import pytest

from aegis.interaction.calls import CallError, TwilioCall, WebhookCall, call_provider_from_settings
from aegis.interaction.notify import ReminderDispatcher, spoken_text
from aegis.planning.reminders import REMINDER_CHANNELS, Reminder, SqlReminderStore
from aegis.planning.schedule import WhenNotParsed, parse_when

NOW = datetime(2026, 9, 5, 15, 0, tzinfo=ZoneInfo("Europe/Moscow"))
TZ = "Europe/Moscow"


def lead(phrase: str) -> datetime:
    return parse_when(phrase, now=NOW, timezone=TZ).at


class TestLeadParsing:
    def test_minus_minutes_from_explicit_time(self) -> None:
        assert lead("за 15 минут до завтра в 15:00") == datetime(
            2026, 9, 6, 14, 45, tzinfo=ZoneInfo(TZ)
        )

    def test_bare_unit_means_one(self) -> None:
        # «за час до» без числа — разговорная норма: 1 час, не отказ и не 30 минут
        assert lead("за час до завтра в 10:00") == datetime(2026, 9, 6, 9, 0, tzinfo=ZoneInfo(TZ))

    def test_half_hours_beats_halfword_prefix(self) -> None:
        assert lead("за полчаса до завтра в 9") == datetime(2026, 9, 6, 8, 30, tzinfo=ZoneInfo(TZ))
        assert lead("за полтора часа до завтра в 12:00") == datetime(
            2026, 9, 6, 10, 30, tzinfo=ZoneInfo(TZ)
        )

    def test_days_and_weeks(self) -> None:
        assert lead("за 2 дня до 12.09 в 10:00") == datetime(
            2026, 9, 10, 10, 0, tzinfo=ZoneInfo(TZ)
        )
        assert lead("за неделю до 20.09 в 12:00") == datetime(
            2026, 9, 13, 12, 0, tzinfo=ZoneInfo(TZ)
        )

    def test_lead_shifts_not_reanchors(self) -> None:
        # якорь «через 3 часа» = 18:00; вычитание — обязательное, а не косметическое
        assert lead("за 20 минут до через 3 часа") == NOW + timedelta(hours=3, minutes=-20)

    def test_lead_without_anchor_is_refused(self) -> None:
        # «до» требует события; молча проигнорировать «за 10 минут до» = поставить не туда
        with pytest.raises(WhenNotParsed):
            parse_when("за 10 минут до", now=NOW, timezone=TZ)
        with pytest.raises(WhenNotParsed):
            parse_when("за минуту до чего-то странного", now=NOW, timezone=TZ)

    def test_chain_and_past_are_refused(self) -> None:
        with pytest.raises(WhenNotParsed):
            parse_when("за час до за 10 минут до завтра в 12:00", now=NOW, timezone=TZ)
        with pytest.raises(WhenNotParsed):
            # вычитание уводит в прошлое: 15:00 + 0 минут (уже наступило)
            parse_when("за час до через 0 минут", now=NOW, timezone=TZ)


class TestLabel:
    def _rem(self, channel: str) -> Reminder:
        return Reminder(
            id="x",
            text="позвонить маме",
            due_at=datetime.now(UTC),
            channel=channel,
        )

    def test_channels_are_declared(self) -> None:
        assert REMINDER_CHANNELS == ("message", "call", "both")

    def test_call_badge(self) -> None:
        assert "📞" in self._rem("call").label()
        assert "📞" in self._rem("both").label()
        assert "📞" not in self._rem("message").label()


class _Recorder:
    """Транспорт-шпион: помитчим, что реально ушло в сеть."""

    def __init__(self, response: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _twilio(rec: _Recorder) -> TwilioCall:
    return TwilioCall(
        account_sid="AC123",
        auth_token="token",
        from_number="+15550001111",
        timeout_s=5,
        transport=rec.transport,
    )


@pytest.mark.asyncio
class TestProviders:
    async def test_twilio_sends_escaped_twiml(self) -> None:
        rem = Reminder(
            id="x",
            text='Созвон "важный" & срочный',
            due_at=datetime(2026, 9, 6, 14, 45, tzinfo=UTC),
        )
        rec = _Recorder(httpx.Response(201, json={"sid": "CA123"}))
        await _twilio(rec).call("+79991234567", spoken_text(rem, "UTC"))
        request = rec.requests[0]
        assert request.url.path.endswith("/Calls.json")
        assert dict(request.headers)["authorization"].startswith("Basic ")
        form = dict(parse_qsl(request.content.decode()))
        assert form["To"] == "+79991234567"
        assert form["From"] == "+15550001111"
        twiml = form["Twiml"]
        # критично именно &<>: голый & сломал бы XML; кавычки в тексте элемента безопасны
        assert "&amp;" in twiml and " < " not in twiml.replace("<Response>", "").replace("<Say", "")
        assert "Созвон" in twiml and "<Say" in twiml and "language=" in twiml
        # дата прописью и «часов» — не «14:45» цифрами
        assert "часов" in twiml

    async def test_twilio_http_error_becomes_callerror(self) -> None:
        rec = _Recorder(httpx.Response(400, json={"message": "Invalid 'To' number"}))
        with pytest.raises(CallError, match="400"):
            await _twilio(rec).call("+7999", "текст")

    async def test_webhook_payload_contract(self) -> None:
        rec = _Recorder(httpx.Response(200, json={"ok": True}))
        provider = WebhookCall(
            url="https://example.test/call", timeout_s=5, transport=rec.transport
        )
        await provider.call("+79991234567", "позвоните мне")
        body = json.loads(rec.requests[0].content.decode())
        assert body == {"to": "+79991234567", "text": "позвоните мне", "language": "ru-RU"}

    async def test_spoken_time_words(self) -> None:
        rem = Reminder(id="x", text="зубной", due_at=datetime(2026, 9, 6, 9, 0, tzinfo=UTC))
        text = spoken_text(rem, "UTC")
        assert "9 часов" in text and "зубной" in text
        rem5 = Reminder(id="x", text="зубной", due_at=datetime(2026, 9, 6, 9, 5, tzinfo=UTC))
        assert "5 минут" in spoken_text(rem5, "UTC")


class _Cfg:
    """Кусок Settings, который читают провайдер и диспетчер."""

    def __init__(self, **kw: Any) -> None:
        self.call_provider = kw.get("call_provider", "none")
        self.notify_phone = kw.get("notify_phone", "")
        self.twilio_account_sid = kw.get("twilio_account_sid", "sid")
        self.twilio_auth_token = kw.get("twilio_auth_token")
        self.twilio_from_number = kw.get("twilio_from_number", "+1")
        self.call_webhook_url = kw.get("call_webhook_url", "")
        self.call_timeout_seconds = 5


class TestProviderFromSettings:
    def test_none_when_unconfigured(self) -> None:
        assert call_provider_from_settings(_Cfg()) is None

    def test_none_when_twilio_incomplete(self) -> None:
        cfg = _Cfg(call_provider="twilio", notify_phone="+79991234567", twilio_auth_token=None)
        assert call_provider_from_settings(cfg) is None

    def test_webhook_makes_provider(self) -> None:
        cfg = _Cfg(
            call_provider="webhook", notify_phone="+7999", call_webhook_url="https://x.test/c"
        )
        assert isinstance(call_provider_from_settings(cfg), WebhookCall)


class _FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str | None]] = []

    async def send(self, reminder: Reminder, prefix: str | None = None) -> None:
        self.sent.append((str(reminder.text), prefix))

    async def start(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


class _FakeCall:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    async def call(self, to: str, text: str) -> None:
        if self.fail:
            raise CallError("провайдер сказал нет")
        self.calls.append((to, text))


def _dispatcher(**kw: Any) -> ReminderDispatcher:
    return ReminderDispatcher(
        telegram=kw.get("telegram") or _FakeTelegram(),
        calls=kw.get("calls"),
        phone=kw.get("phone", "+79991234567"),
        timezone="UTC",
    )


def _rem(channel: str) -> Reminder:
    return Reminder(
        id="i", text="страница «1001» — почистить", due_at=datetime.now(UTC), channel=channel
    )


@pytest.mark.asyncio
class TestDispatcher:
    async def test_message_never_touches_phone(self) -> None:
        call = _FakeCall()
        d = _dispatcher(calls=call)
        await d.send(_rem("message"))
        assert call.calls == [] and len(d.telegram.sent) == 1 and d.last_notes == []

    async def test_call_success_sends_no_message(self) -> None:
        call = _FakeCall()
        d = _dispatcher(calls=call)
        await d.send(_rem("call"))
        assert len(call.calls) == 1 and d.telegram.sent == [] and d.last_notes == []
        assert call.calls[0][0] == "+79991234567"

    async def test_call_failure_falls_back_with_confession(self) -> None:
        d = _dispatcher(calls=_FakeCall(fail=True))
        await d.send(_rem("call"))
        ((text, prefix),) = d.telegram.sent
        assert prefix is not None and "Не дозвонился" in prefix
        assert d.last_notes and "звонок не удался" in d.last_notes[0]

    async def test_both_is_message_then_call(self) -> None:
        call = _FakeCall()
        d = _dispatcher(calls=call)
        await d.send(_rem("both"))
        assert len(d.telegram.sent) == 1 and len(call.calls) == 1
        assert d.telegram.sent[0][1] is None  # сообщение both — без «исповедального» префикса

    async def test_both_call_failure_keeps_message_and_notes(self) -> None:
        d = _dispatcher(calls=_FakeCall(fail=True))
        await d.send(_rem("both"))
        assert len(d.telegram.sent) == 1 and d.last_notes

    async def test_unconfigured_call_degrades_to_message_once(self) -> None:
        d = _dispatcher(calls=None)
        await d.send(_rem("call"))
        assert len(d.telegram.sent) == 1
        (text, prefix) = d.telegram.sent[0]
        assert prefix is not None and "не настроены" in prefix
        assert d.last_notes == ["i: звонок заказан, но не настроен"]

    async def test_notes_reset_between_sends(self) -> None:
        d = _dispatcher(calls=None)
        await d.send(_rem("call"))
        assert d.last_notes
        await d.send(_rem("message"))
        assert d.last_notes == []


class TestStoreChannelGate:
    @pytest.mark.asyncio
    async def test_bad_channel_never_reaches_sql(self) -> None:
        # отказ до первого запроса: сессия=None здесь лакмус — до БД дело не доходит
        store = SqlReminderStore()
        with pytest.raises(ValueError, match="канал доставки"):
            await store.add(
                owner_id=1, body="письмо в налоговую", due_at=datetime.now(UTC), channel="sms"
            )
