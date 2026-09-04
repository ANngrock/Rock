"""Курсы валют: детерминированный путь — разбор вопроса, источники, сверка и кэш.

Сеть подменяется httpx.MockTransport: проверяем решение (что показать, что назвать сверенным, а что
— расхождением), а не доброту ПриватБанка.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from aegis.web.rates import (
    RateAnswer,
    RateQuestion,
    RateQuote,
    fetch_rates,
    parse_rate_question,
    rates_cache_key,
)

PRIVAT_ROWS = [
    {"ccy": "USD", "base_ccy": "UAH", "buy": "41.30000", "sale": "41.75000"},
    {"ccy": "EUR", "base_ccy": "UAH", "buy": "48.10000", "sale": "48.60000"},
    {"ccy": "PLN", "base_ccy": "UAH", "buy": "11.20000", "sale": "11.60000"},
]
NBU_ROWS = [
    {
        "r030": 840,
        "cc": "USD",
        "txt": "Долар США",
        "rate": 41.4,
        "units": 1,
        "exchangedate": "04.09.2026",
    },
    {
        "r030": 978,
        "cc": "EUR",
        "txt": "Євро",
        "rate": 48.3,
        "units": 1,
        "exchangedate": "04.09.2026",
    },
]


def transport(*, fail: tuple[str, ...] = ()) -> httpx.AsyncClient:
    """Клиент, который знает все три источника и умеет их валить по имени хоста."""

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if host in fail:
            raise httpx.ConnectError(f"{host} недоступен")
        if "privatbank" in host:
            return httpx.Response(200, json=PRIVAT_ROWS)
        if "bank.gov.ua" in host:
            return httpx.Response(200, json=NBU_ROWS)
        if "er-api" in host:
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "base_code": "USD",
                    "time_last_update_utc": "Thu, 04 Sep 2026 21:00:00 +0000",
                    "rates": {"UAH": 41.5},
                },
            )
        return httpx.Response(404, text="нет такого источника")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ разбор вопроса


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("скажи мне актуальный курс доллара в приватбанке", ("pair", "USD", "UAH")),
        ("курс доллара к гривне", ("pair", "USD", "UAH")),
        ("курс евро к доллару в германии", ("pair", "EUR", "USD")),
        ("курс usd/uah", ("pair", "USD", "UAH")),
        ("курс злотого в обменнике", ("pair", "PLN", "UAH")),
        ("курс доллара и евро", ("table", "USD", "UAH")),
        ("сколько стоит гривна", ("table", "UAH", "UAH")),
    ],
)
def test_rate_questions_are_recognised(text: str, expected: tuple[str, str, str]) -> None:
    question = parse_rate_question(text)
    assert question is not None
    assert (question.mode, question.base, question.quote) == expected


@pytest.mark.parametrize(
    "text",
    [
        "столица германии",
        "запомни: рост 182",
        "добавь расход 12 евро за такси",
        "привет",
        "",
    ],
)
def test_everything_else_is_left_to_the_model(text: str) -> None:
    """Детерминированный путь не имеет права перехватывать произвольные сообщения."""
    assert parse_rate_question(text) is None


def test_cash_wording_selects_the_right_office() -> None:
    assert parse_rate_question("наличный курс доллара").cash is True  # type: ignore[union-attr]
    assert (
        parse_rate_question("курс доллара по карте").cash is False  # type: ignore[union-attr]
    )
    assert parse_rate_question("курс доллара").cash is None  # type: ignore[union-attr]


# --------------------------------------------------------------------- сверка


async def test_two_sources_agree_and_answer_is_rendered() -> None:
    answer = await fetch_rates(
        RateQuestion(base="USD", quote="UAH"),
        client=transport(),
        cfg=_cfg(rate_sources="privatbank,nbu"),
    )
    assert answer.verdict == "agreed", answer.causes
    kinds = {quote.kind for quote in answer.quotes}
    assert {"bank_cashless", "bank_cash", "official"} <= kinds, "сверять надо с официальным курсом"
    text = answer.render()
    assert "41." in text and "НБУ" in text and "ПриватБанк" in text
    assert "расхождение" in text


async def test_bank_margin_is_reported_not_hidden() -> None:
    """Банк всегда чуть дальше официального — это маржа, и её надо показать, а не проглотить."""
    nbu_off = [{"r030": 840, "cc": "USD", "rate": 39.0, "units": 1, "exchangedate": "04.09.2026"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if "bank.gov.ua" in (request.url.host or ""):
            return httpx.Response(200, json=nbu_off)
        return httpx.Response(200, json=PRIVAT_ROWS)

    answer = await fetch_rates(
        RateQuestion(base="USD", quote="UAH"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        cfg=_cfg(rate_sources="privatbank,nbu"),
    )
    assert answer.verdict == "conflict", answer.deviation_pct
    assert "сверься на сайте банка" in answer.render()
    assert answer.official_gap_pct and answer.official_gap_pct > 5


async def test_single_source_is_marked_unverified() -> None:
    answer = await fetch_rates(
        RateQuestion(base="USD", quote="UAH"), client=transport(), cfg=_cfg(rate_sources="nbu")
    )
    assert answer.verdict == "single_source"
    assert "один источник, не сверено" in answer.render()


async def test_missing_pair_is_not_silently_invented() -> None:
    """Нет такой пары у источника — значит «нет», а не «возьмём что-нибудь похожее»."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[{"ccy": "USD", "base_ccy": "UAH", "buy": "1", "sale": "2"}]
        )

    answer = await fetch_rates(
        RateQuestion(base="GBP", quote="UAH"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        cfg=_cfg(rate_sources="privatbank"),
    )
    assert answer.verdict == "unavailable"
    assert any("GBP" in cause for cause in answer.causes)


async def test_dead_sources_produce_a_diagnosis_not_silence() -> None:
    answer = await fetch_rates(
        RateQuestion(base="USD", quote="UAH"),
        client=transport(fail=("api.privatbank.ua", "bank.gov.ua", "open.er-api.com")),
        cfg=_cfg(rate_sources="privatbank,nbu"),
    )
    assert answer.verdict == "unavailable"
    assert "не ответил" in answer.render() or "не получил" in answer.render()
    assert answer.causes, "причины обязаны быть, иначе владелец будет гадать"


# ------------------------------------------------------------------ кросс и таблица


async def test_cross_rate_is_computed_and_declared() -> None:
    """EUR/USD: банка с таким котированием нет — считаем через гривну и честно пишем об этом."""
    answer = await fetch_rates(
        RateQuestion(base="EUR", quote="USD"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=PRIVAT_ROWS))
        ),
        cfg=_cfg(rate_sources="privatbank"),
    )
    value = next(q.mid for q in answer.quotes if q.mid)
    assert value and 1.0 < value < 1.4, f"пересчёт EUR/USD через UAH: {value}"
    assert "пересчёт через гривну" in answer.render()


async def test_table_mode_answers_the_currencies_that_were_named() -> None:
    question = parse_rate_question("курс доллара и евро")
    assert question is not None
    answer = await fetch_rates(
        question, client=transport(), cfg=_cfg(rate_sources="privatbank,nbu")
    )
    assert set(answer.table) == {"USD", "EUR"}
    assert "USD:" in answer.render() and "EUR:" in answer.render()


async def test_table_mode_makes_one_call_per_source() -> None:
    """Список валют ≠ список запросов: иначе «сколько стоит гривна» упиралось бы в таймаут."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "privatbank" in (request.url.host or ""):
            return httpx.Response(200, json=PRIVAT_ROWS)
        return httpx.Response(200, json=NBU_ROWS)

    question = parse_rate_question("курс доллара и евро")
    assert question is not None
    answer = await fetch_rates(
        question,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        cfg=_cfg(rate_sources="privatbank,nbu"),
    )
    assert len(calls) == 3, f"ожидалось 2 фида Привата + 1 НБУ, получилось {calls}"
    assert answer.verdict != "unavailable"


# ------------------------------------------------------------------------- кэш


class FakeKV:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self.data.get(key)

    async def set(self, key: str, value: Any, *, ex: int | None = None) -> None:
        self.data[key] = value if isinstance(value, (bytes, str)) else str(value)
        self.ttl = ex


async def test_cache_answers_the_repeat_without_network() -> None:
    kv = FakeKV()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=PRIVAT_ROWS)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    question = RateQuestion(base="USD", quote="UAH")
    cfg = _cfg(rate_sources="privatbank", rate_cache_ttl_s=300)
    first = await fetch_rates(question, client=client, cfg=cfg, kv=kv)
    after_first = len(calls)
    second = await fetch_rates(question, client=client, cfg=cfg, kv=kv)
    assert first.from_cache is False and second.from_cache is True
    assert len(calls) == after_first, "повтор не должен дёргать банк заново"
    assert second.quotes[0].buy == first.quotes[0].buy
    assert "из кэша" in second.render()


async def test_cache_key_follows_the_question() -> None:
    card = rates_cache_key(RateQuestion(base="USD", quote="UAH", cash=False), _cfg())
    cash = rates_cache_key(RateQuestion(base="USD", quote="UAH", cash=True), _cfg())
    assert card != cash, "наличные и карта — разные ответы, кэш не должен их смешивать"


async def test_broken_cache_does_not_break_the_answer() -> None:
    class ExplodingKV:
        async def get(self, key: str) -> Any:
            raise ConnectionError("redis лёг")

        async def set(self, key: str, value: Any, *, ex: int | None = None) -> None:
            raise ConnectionError("redis лёг")

    answer = await fetch_rates(
        RateQuestion(base="USD", quote="UAH"),
        client=transport(),
        cfg=_cfg(rate_sources="privatbank", rate_cache_ttl_s=60),
        kv=ExplodingKV(),
    )
    assert answer.verdict != "unavailable", "отказ кэша не имеет права обнулять ответ"


def test_render_carries_the_provenance() -> None:
    """Воспроизводимость: видно источник, число и когда снято — без похода в базу."""
    answer = RateAnswer(
        question=RateQuestion(base="USD", quote="UAH"),
        quotes=[
            RateQuote(
                source="ПриватБанк · безналичный",
                url="https://api.privatbank.ua/x",
                base="USD",
                quote="UAH",
                buy=41.3,
                sell=41.75,
                kind="bank_cashless",
            )
        ],
        verdict="single_source",
        fetched_at="2026-09-05T01:13:00+03:00",
    )
    text = answer.render()
    assert "2026-09-05 01:13" in text
    assert "покупка 41.3000 · продажа 41.7500" in text
    meta = answer.as_meta()
    assert meta["sources"][0]["url"].startswith("https://") and meta["verdict"] == "single_source"


def _cfg(**over: Any) -> Any:
    """Конфиг прогона: только те настройки, от которых зависит решение парсера и сверки."""
    from aegis.platform.config import Settings

    values: dict[str, Any] = {
        "rate_tolerance_pct": 1.5,
        "rate_home_currency": "UAH",
        "rate_cache_ttl_s": 0,
        "rate_sources": "privatbank,nbu,erapi",
    }
    values.update(over)
    return Settings(_env_file=None, _env_prefix="T_", **values)
