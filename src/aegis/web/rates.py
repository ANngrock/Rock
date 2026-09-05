"""Курсы валют: детерминированный путь без LLM — публичные источники и сверка между ними.

Зачем это отдельным путём, а не «попроси модель погуглить»: вопрос «курс доллара в приватбанке»
имеет точный ответ, который можно снять с самого банка за 200 мс и сверить с официальным курсом НБУ.
Такой путь работает при выключенных моделях, при пустом бюджете и при мёртвом SearXNG — то есть
реализует принцип «детерминированные парсеры работают всегда» (часть I плана, принцип 5).

Никаких ключей: ПриватБанк отдаёт публичный XML/JSON-фид курсов, НБУ — официальный статистический.
Каждый источник помечен, поэтому в ответе видно, чьё именно число показано, когда оно снято и чем
оно отличается от официального.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from aegis.platform.config import Settings, settings

__all__ = [
    "RateAnswer",
    "RateQuestion",
    "RateQuote",
    "RatesUnavailable",
    "fetch_rates",
    "parse_rate_question",
    "rates_cache_key",
]

log = structlog.get_logger(__name__)

_WS_RX = re.compile(r"\s+")

#: Коды и их русские/украинские/английские названия в формах, которые реально пишут.
_CURRENCIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("USD", ("доллар", "долар", "usd", "buck")),
    ("EUR", ("евро", "євро", "eur")),
    ("UAH", ("гривн", "гривец", "гривен", "uah", "hryvnia", "hryvnya")),
    ("PLN", ("злот", "плн", "polish zloty", "zloty", "zlot")),
    ("GBP", ("фунт", "gbp", "стерлинг")),
    ("CNY", ("юан", "cny", "жэньминьби")),
    ("TRY", ("лир", "try", "турецк")),
    ("CHF", ("франк", "chf")),
    ("RUB", ("рубл", "rub")),
    ("KZT", ("тенге", "kzt")),
    ("BTC", ("биткоин", "bitcoin", "btc")),
    ("MDL", ("лей", "молдавск", "mdl")),
)
_SYMBOLS: tuple[tuple[str, str], ...] = (("$", "USD"), ("€", "EUR"), ("₴", "UAH"))

#: Признать сообщение «вопросом про курс» можно только по этим маркерам: иначе детерминированный
#: путь начнёт перехватывать произвольные сообщения владельца.
_RATE_HINT_RX = re.compile(
    r"(курс\w*|по сколько|скольк\w* стоит|обмен\w*|конвертац\w*|买|курс валют|rate|exchange)",
    re.IGNORECASE,
)
#: вопрос о курсе можно задать и без слова «курс»: «сколько сейчас евро», «почём доллар»
_ASK_RX = re.compile(
    r"(\bсколько\w*|\bпоч[ёе]м\b|\bпо скольку\b|\bза сколько\b|\bпо какой цене\b"
    r"|\bсколько стоит\b|\bhow much\b)",
    re.IGNORECASE,
)
#: сообщение, где владелец что-то записывает или просит сделать, — не про курс. Перехватить его
#: означало бы ответить числом вместо того, чтобы отдать решение policy engine.
_WRITE_RX = re.compile(
    r"(добав|запиш|вн[ёе]с|потрат|приход|расход|перевед|оплат|запомн|удали|измен"
    r"|напомн|создай|запланир|учт[иё]|поставь)",
    re.IGNORECASE,
)
_CASH_RX = re.compile(r"(наличн|обменник|в отделени|касс\w*|cash)", re.IGNORECASE)
_CASHLESS_RX = re.compile(
    r"(карт\w*|безналичн|приват24|monobank|перевод\w*|cashless)", re.IGNORECASE
)
_RELATION_RX = re.compile(r"(\sк\s|\sпротив\s|\sотносительно\s|\svs\.?\s|/|→)")
_BANK_RX = re.compile(
    r"(приват\w*|моно\s*банк|monobank|пумб|ощад\w*|альфа|а-банк|raiffeisen|райф\w*"
    r"|универсал\w*|creditwest)",
    re.IGNORECASE,
)

#: что показываем, когда валюту не назвали («сколько стоит гривна»)
_TABLE_DEFAULT: tuple[str, ...] = ("USD", "EUR", "PLN", "GBP")
_PRIVAT_URL = "https://api.privatbank.ua/p24api/pubinfo?json&exchange&coursid={csid}"
_NBU_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"
_ERAPI_URL = "https://open.er-api.com/v6/latest/{base}"
#: «безготівковий» (карточки, Приват24) — то, что человек реально получает, платя картой.
_PRIVAT_CASHLESS = "11"
#: кассовые обменные операции — наличные в отделении.
_PRIVAT_CASH = "5"
#: csid → вид котировки: у наличных и безналичных разные смысл и разные числа
_PRIVAT_KIND = {_PRIVAT_CASHLESS: "bank_cashless", _PRIVAT_CASH: "bank_cash"}


class RatesUnavailable(RuntimeError):
    """Ни один источник курсов не ответил (причины — в атрибуте `causes`)."""

    def __init__(self, *args: str, causes: list[str] | None = None) -> None:
        super().__init__(*args)
        self.causes = causes or []


@dataclass(frozen=True, slots=True)
class RateQuestion:
    """Что владелец спросил: пара, режим и контекст (наличные/безналичный, банк)."""

    base: str
    quote: str
    cash: bool | None = None
    bank: str = ""
    mode: str = "pair"  # pair | table
    #: для table-режима: что именно перечислил владелец («доллар и евро» — это не пара)
    codes: tuple[str, ...] = ()
    raw: str = ""

    @property
    def label(self) -> str:
        return f"{self.base}/{self.quote}"


@dataclass(frozen=True, slots=True)
class RateQuote:
    """Одно число из одного источника. `buy`/`sell` — в валюте `quote` за 1 единицу `base`."""

    source: str
    url: str
    base: str
    quote: str
    buy: float | None = None
    sell: float | None = None
    as_of: str = ""
    kind: str = "bank"  # bank_cashless | bank_cash | official | aggregator
    note: str = ""

    @property
    def mid(self) -> float | None:
        values = [value for value in (self.buy, self.sell) if value]
        if not values:
            return None
        return round(sum(values) / len(values), 6)

    def render_line(self) -> str:
        parts = [f"{self.source}"]
        if self.buy is not None and self.sell is not None:
            parts.append(f"покупка {self.buy:.4f} · продажа {self.sell:.4f}")
        elif self.buy is not None:
            parts.append(f"{self.buy:.4f}")
        elif self.sell is not None:
            parts.append(f"{self.sell:.4f}")
        else:
            parts.append("числа не отдал")
        if self.as_of:
            parts.append(f"у источника дата {self.as_of}")
        return " — ".join(parts) + (f" ({self.note})" if self.note else "")


@dataclass(slots=True)
class RateAnswer:
    question: RateQuestion
    quotes: list[RateQuote] = field(default_factory=list)
    verdict: str = "single_source"  # agreed | spread_noted | single_source | conflict | unavailable
    deviation_pct: float = 0.0
    official_gap_pct: float | None = None
    table: dict[str, list[RateQuote]] = field(default_factory=dict)
    fetched_at: str = ""
    from_cache: bool = False
    causes: list[str] = field(default_factory=list)

    def as_meta(self) -> dict[str, Any]:
        """Для трассы/события: чем ответили и на основании чего (принцип 4 — воспроизводимость)."""
        return {
            "question": self.question.label,
            "mode": self.question.mode,
            "verdict": self.verdict,
            "deviation_pct": round(self.deviation_pct, 3),
            "official_gap_pct": (
                round(self.official_gap_pct, 3) if self.official_gap_pct is not None else None
            ),
            "sources": [
                {
                    "source": quote.source,
                    "kind": quote.kind,
                    "buy": quote.buy,
                    "sell": quote.sell,
                    "as_of": quote.as_of,
                    "url": quote.url,
                }
                for quote in self.quotes
            ],
            "causes": self.causes,
            "fetched_at": self.fetched_at,
            "from_cache": self.from_cache,
        }

    def render(self, *, home: str = "UAH") -> str:
        """Ответ владельцу: числа, источники, дата снятия и что с этим делать, если разошлись."""
        when = self.fetched_at[:16].replace("T", " ") if self.fetched_at else "сейчас"
        cache_note = " · из кэша" if self.from_cache else ""
        if self.verdict == "unavailable":
            return (
                "Курсы не получил — ни один источник не ответил.\n"
                + "\n".join(f"· {cause}" for cause in collapse_causes(self.causes)[:4])
                + f"\nПроверка: aegis doctor (раздел rates){cache_note}"
            )
        if self.question.mode == "table":
            lines = [f"Курс к {self.question.quote.upper()} — источники: {when}{cache_note}"]
            for code, quotes in self.table.items():
                best = next((q for q in quotes if q.kind == "bank_cashless"), quotes[0])
                official = next((q for q in quotes if q.kind == "official"), None)
                value = best.mid
                text = f"{code}: {value:.4f}" if value else f"{code}: нет числа"
                if official and official.mid and value:
                    text += f" (офиц. НБУ {official.mid:.4f})"
                lines.append(text)
            return "\n".join(lines)

        lines = [f"{self.question.label} — {when}{cache_note}"]
        for quote in self.quotes:
            lines.append("· " + quote.render_line())
        if self.verdict == "agreed":
            lines.append(
                f"Источники согласуются: расхождение {self.deviation_pct:.2f} % (допуск "
                f"{settings().rate_tolerance_pct:.2f} %)."
            )
        elif self.verdict == "spread_noted":
            lines.append(
                f"Банк отличается от официального на {self.official_gap_pct:.2f} % — это обычная "
                f"маржа обмена, не ошибка данных."
                if self.official_gap_pct is not None
                else (
                    f"Источники расходятся на {self.deviation_pct:.2f} % — смотри дату у источника."
                )
            )
        elif self.verdict == "conflict":
            lines.append(
                f"Источники одного типа расходятся на {self.deviation_pct:.2f} % — число может быть"
                " устаревшим, сверься на сайте банка."
            )
        elif self.verdict == "single_source":
            lines.append("Данных больше не от кого: это один источник, не сверено.")
        if self.question.cash is None:
            lines.append(
                "Показал безналичный и кассовый: для карты важен первый, для обменника — второй."
                if len({q.kind for q in self.quotes if q.kind.startswith("bank")}) > 1
                else "Уточни, что нужно: наличные в кассе или оплата картой."
            )
        if self.question.quote.upper() != home.upper() and self.question.mode == "pair":
            lines.append("Кросс-курс — пересчёт через гривну, прямого котирования у источника нет.")
        return "\n".join(lines)


def _norm(text: str) -> str:
    return _WS_RX.sub(" ", (text or "").strip().lower())


def _find_currencies(text: str) -> list[tuple[str, int]]:
    """(код, позиция) — по названиям и символам, без повторов одного и того же кода рядом."""
    found: list[tuple[str, int]] = []
    for code, needles in _CURRENCIES:
        for needle in needles:
            for match in re.finditer(re.escape(needle), text):
                found.append((code, match.start()))
                break  # одно вхождение названия достаточно
    for symbol, code in _SYMBOLS:
        index = (text or "").find(symbol)
        if index >= 0:
            found.append((code, index))
    found.sort(key=lambda pair: pair[1])
    deduped: list[tuple[str, int]] = []
    for code, _pos in found:
        if not deduped or deduped[-1][0] != code:
            deduped.append((code, _pos))
    return deduped


def parse_rate_question(text: str, *, home: str = "UAH") -> RateQuestion | None:
    """Распознать вопрос о курсе. None — «не моя тема», тогда отвечают модель и поиск.

    Порядок валют важен: «евро к доллару» — это EUR/USD, а не USD/EUR. Отвечаем только на то, где
    явно названа валюта и есть маркер вопроса про курс: иначе детерминированный путь перехватывал
    бы произвольные сообщения.
    """
    raw = (text or "").strip()
    normalized = _norm(raw)
    if not normalized:
        return None
    if _WRITE_RX.search(normalized):
        # «запиши: держим курс EUR/USD 41.5» — это заметка или расход, а не вопрос о курсе
        return None
    codes = {code for code, _needles in _CURRENCIES}

    direct = re.search(
        r"\b([a-z]{3})\s*(?:/|→|к\s+|против\s+|относительно\s+)\s*([a-z]{3})\b", normalized
    )
    if direct and direct.group(1).upper() in codes and direct.group(2).upper() in codes:
        base, quote = direct.group(1).upper(), direct.group(2).upper()
    else:
        found = _find_currencies(normalized)
        if not found:
            return None
        # «евро к доллару» — вопроса нет, но порядок явно задан отношением: это тоже про курс
        relation = len(dict.fromkeys(code for code, _ in found)) > 1 and bool(
            _RELATION_RX.search(normalized)
        )
        if not (_RATE_HINT_RX.search(normalized) or _ASK_RX.search(normalized) or relation):
            return None
        if len(found) == 1:
            code = found[0][0]
            if code == home.upper():
                # «курс гривны» — сами по себе бессмыслица: показываем список к гривне
                return RateQuestion(
                    base=home.upper(),
                    quote=home.upper(),
                    cash=_cash_mode(raw),
                    bank=_bank(raw),
                    mode="table",
                    raw=raw,
                )
            base, quote = code, home.upper()
        else:
            wanted = tuple(dict.fromkeys(code for code, _ in found))
            if not _RELATION_RX.search(normalized):
                # «доллар и евро» — это два вопроса, а не кросс-курс: врать про EUR/USD незачем
                return RateQuestion(
                    base=wanted[0],
                    quote=home.upper(),
                    cash=_cash_mode(raw),
                    bank=_bank(raw),
                    mode="table",
                    codes=wanted,
                    raw=raw,
                )
            base, quote = wanted[0], wanted[1]
    return RateQuestion(
        base=base, quote=quote, cash=_cash_mode(raw), bank=_bank(raw), mode="pair", raw=raw
    )


def _cash_mode(text: str) -> bool | None:
    if _CASH_RX.search(text or ""):
        return True
    if _CASHLESS_RX.search(text or ""):
        return False
    return None


def _bank(text: str) -> str:
    match = _BANK_RX.search(text or "")
    return match.group(1).casefold() if match else ""


def rates_cache_key(question: RateQuestion, cfg: Settings | None = None) -> str:
    cfg = cfg or settings()
    suffix = "cash" if question.cash else ("card" if question.cash is False else "both")
    return f"rates:{question.base}/{question.quote}:{suffix}:{cfg.rate_sources or ''}"[:120]


async def _get_json(client: httpx.AsyncClient, url: str) -> Any:
    resp = await client.get(url, headers={"Accept": "application/json", "User-Agent": "aegis/0.1"})
    if resp.status_code != 200:
        raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
    return resp.json()


class RateSources:
    """Сбор чисел из источников. Отдельный класс — чтобы тесты подменяли transport, а не сеть."""

    def __init__(
        self, client: httpx.AsyncClient | None = None, *, cfg: Settings | None = None
    ) -> None:
        self._client = client
        self.cfg = cfg or settings()

    async def quotes(self, question: RateQuestion) -> tuple[list[RateQuote], list[str]]:
        """(числа, причины отказов): отказ источника не должен становиться «ничего не нашлось»."""
        cfg = self.cfg
        enabled = [
            name.strip().casefold() for name in (cfg.rate_sources or "").split(",") if name.strip()
        ]
        client = self._client or httpx.AsyncClient(timeout=cfg.rates_timeout_s)
        owns = self._client is None
        out: list[RateQuote] = []
        causes: list[str] = []
        try:
            for name in enabled:
                try:
                    if name == "privatbank":
                        out.extend(await self._privatbank(client, question, cfg))
                    elif name == "nbu":
                        quote = await self._nbu(client, question)
                        if quote:
                            out.append(quote)
                    elif name == "erapi":
                        quote = await self._erapi(client, question)
                        if quote:
                            out.append(quote)
                    else:
                        causes.append(f"{name}: неизвестный источник в RATE_SOURCES")
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    causes.append(f"{name}: {type(exc).__name__} {str(exc)[:120]}".strip())
        finally:
            if owns:
                await client.aclose()
        return out, causes

    async def batch(
        self, question: RateQuestion, codes: tuple[str, ...] | list[str]
    ) -> tuple[list[RateQuote], list[str]]:
        """Один поход в источник на весь список валют.

        Без этого «сколько стоит гривна» превращалось бы в двенадцать последовательных запросов к
        банку — при SLO p50 1.5 с это не деградация, а отказ по таймауту.
        """
        cfg = self.cfg
        enabled = [
            name.strip().casefold() for name in (cfg.rate_sources or "").split(",") if name.strip()
        ]
        client = self._client or httpx.AsyncClient(timeout=cfg.rates_timeout_s)
        owns = self._client is None
        out: list[RateQuote] = []
        causes: list[str] = []
        try:
            for name in enabled:
                try:
                    if name == "privatbank":
                        out.extend(await self._privatbank_batch(client, question, list(codes)))
                    elif name == "nbu":
                        rows = await _get_json(client, _NBU_URL)
                        if not isinstance(rows, list):
                            raise ValueError("НБУ вернул не список")
                        for code in codes:
                            quote = _pick_nbu(rows, code)
                            if quote:
                                out.append(quote)
                    elif name == "erapi":
                        continue  # у агрегатора курс «одна база → все», в таблице он лишнее звено
                    else:
                        causes.append(f"{name}: неизвестный источник в RATE_SOURCES")
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    causes.append(f"{name}: {type(exc).__name__} {str(exc)[:120]}".strip())
        finally:
            if owns:
                await client.aclose()
        return out, causes

    async def _privatbank_batch(
        self, client: httpx.AsyncClient, question: RateQuestion, codes: list[str]
    ) -> list[RateQuote]:
        wanted = (
            [_PRIVAT_CASH]
            if question.cash
            else [_PRIVAT_CASHLESS]
            if question.cash is False
            else [_PRIVAT_CASHLESS, _PRIVAT_CASH]
        )
        out: list[RateQuote] = []
        for csid in wanted:
            data = await _get_json(client, _PRIVAT_URL.format(csid=csid))
            if not isinstance(data, list):
                raise ValueError(f"приват вернул не список (csid={csid})")
            rows = [row for row in data if isinstance(row, dict)]
            for code in codes:
                found = _pick_privat(rows, code.upper(), "UAH")
                if found is None:
                    continue
                buy, sell = found
                out.append(
                    RateQuote(
                        source=f"ПриватБанк · {'касса' if csid == _PRIVAT_CASH else 'безналичный'}",
                        url=_PRIVAT_URL.format(csid=csid),
                        base=code.upper(),
                        quote="UAH",
                        buy=buy,
                        sell=sell,
                        kind=_PRIVAT_KIND[csid],
                    )
                )
        return out

    async def _privatbank(
        self, client: httpx.AsyncClient, question: RateQuestion, cfg: Settings
    ) -> list[RateQuote]:
        urls = _PRIVAT_KIND
        wanted = (
            [_PRIVAT_CASH]
            if question.cash
            else [_PRIVAT_CASHLESS]
            if question.cash is False
            else []
        ) or list(urls)
        out: list[RateQuote] = []
        for csid in wanted:
            data = await _get_json(client, _PRIVAT_URL.format(csid=csid))
            if not isinstance(data, list):
                raise ValueError(f"приват вернул не список (csid={csid})")
            rows = [row for row in data if isinstance(row, dict)]
            for base, quote in self._pairs_needed(question):
                found = _pick_privat(rows, base, quote)
                if found is None:
                    if base == question.base and quote == question.quote:
                        raise ValueError(f"в фиде ПриватБанка нет пары {base}/{quote}")
                    continue
                buy, sell = found
                out.append(
                    RateQuote(
                        source=f"ПриватБанк · {'касса' if csid == _PRIVAT_CASH else 'безналичный'}",
                        url=_PRIVAT_URL.format(csid=csid),
                        base=base,
                        quote=quote,
                        buy=buy,
                        sell=sell,
                        kind=urls[csid],
                        note="пересчёт через гривну"
                        if (base, quote)
                        != (
                            question.base,
                            question.quote,
                        )
                        else "",
                    )
                )
        return out

    def _pairs_needed(self, question: RateQuestion) -> list[tuple[str, str]]:
        if question.quote.upper() == "UAH":
            return [(question.base.upper(), "UAH")]
        # кросс: банку известна только пара к гривне, поэтому считаем из двух котировок
        return [(question.base.upper(), "UAH"), (question.quote.upper(), "UAH")]

    async def _nbu(self, client: httpx.AsyncClient, question: RateQuestion) -> RateQuote | None:
        if question.quote.upper() != "UAH":
            return None
        data = await _get_json(client, _NBU_URL)
        if not isinstance(data, list):
            raise ValueError("НБУ вернул не список")
        return _pick_nbu(data, question.base.upper())

    async def _erapi(self, client: httpx.AsyncClient, question: RateQuestion) -> RateQuote | None:
        url = _ERAPI_URL.format(base=question.base.upper())
        data = await _get_json(client, url)
        if not isinstance(data, dict):
            raise ValueError("open.er-api вернул не объект")
        rates = data.get("rates")
        if not isinstance(rates, dict):
            raise ValueError("open.er-api без блока rates")
        value = _number(rates.get(question.quote.upper()))
        if not value:
            return None
        return RateQuote(
            source="open.er-api · агрегатор",
            url=url,
            base=question.base.upper(),
            quote=question.quote.upper(),
            buy=round(value, 6),
            sell=None,
            as_of=str(data.get("time_last_update_utc") or "")[:22],
            kind="aggregator",
        )


def _pick_nbu(rows: list[Any], code: str) -> RateQuote | None:
    """Строка НБУ → курс за 1 единицу: `rate` там дан на `units` — для мелочи это важно."""
    for row in rows:
        if not isinstance(row, dict) or str(row.get("cc", "")).upper() != code.upper():
            continue
        units = _number(row.get("units")) or 1.0
        rate = _number(row.get("rate"))
        if not rate:
            return None
        value = round(rate / units, 6)
        return RateQuote(
            source="НБУ · официальный",
            url=_NBU_URL,
            base=code.upper(),
            quote="UAH",
            buy=value,
            sell=value,
            as_of=str(row.get("exchangedate") or ""),
            kind="official",
        )
    return None


def _pick_privat(
    rows: list[dict[str, Any]], base: str, quote: str
) -> tuple[float | None, float | None] | None:
    """Котировка Привата за 1 единицу `base` в `quote`; `self` не нужен — разбор чист по данным.

    Вынесен из класса не ради красоты: этот разбор проверяется golden-набором на реальных формах
    ответа (включая обратную котировку, которую приходится переворачивать) — и без сети.
    """
    for row in rows:
        if str(row.get("ccy", "")).upper() != base:
            continue
        if str(row.get("base_ccy", "")).upper() != quote:
            continue
        buy, sell = _number(row.get("buy")), _number(row.get("sale"))
        if buy is None and sell is None:
            return None
        return buy, sell
    # у Привата есть и обратные котировки (UAH/PLN) — попробуем перевернуть
    for row in rows:
        if str(row.get("ccy", "")).upper() != quote:
            continue
        if str(row.get("base_ccy", "")).upper() != base:
            continue
        buy, sell = _number(row.get("buy")), _number(row.get("sale"))
        if not buy or not sell:
            return None
        return (round(1 / sell, 6), round(1 / buy, 6))
    return None


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(str(value).replace(",", ".").strip())
    except ValueError:
        return None
    return number if number > 0 else None


def _derive_crosses(quotes: list[RateQuote], question: RateQuestion) -> list[RateQuote]:
    """Кросс-курс, если источник котирует только к гривне: (base/UAH) ÷ (quote/UAH).

    Покупка и продажа считаются по разным ногам (банк продаёт одну валюту и покупает другую) —
    иначе «спред» вышел бы математически красивым и практически выдуманным.
    """
    legs: dict[str, dict[str, RateQuote]] = {}
    for quote in quotes:
        legs.setdefault(quote.source, {})[quote.base.upper()] = quote
    out: list[RateQuote] = []
    for source, by_code in legs.items():
        left, right = by_code.get(question.base.upper()), by_code.get(question.quote.upper())
        if left is None or right is None:
            continue
        buy = round(left.sell / right.buy, 6) if left.sell and right.buy else None
        sell = round(left.buy / right.sell, 6) if left.buy and right.sell else None
        if buy is None and sell is None:
            continue
        out.append(
            RateQuote(
                source=f"{source} · расчёт",
                url=left.url,
                base=question.base.upper(),
                quote=question.quote.upper(),
                buy=buy,
                sell=sell,
                as_of=left.as_of or right.as_of,
                kind=left.kind,
                note=f"кросс через {left.quote.upper()}",
            )
        )
    return out


def _verdict(quotes: list[RateQuote], *, tolerance: float) -> tuple[str, float, float | None]:
    """Сверка чисел: согласуются / банк ушёл от официального / источники спорят по-настоящему.

    Правило простое и проверяемое: допуск `rate_tolerance_pct` — норма для маржи обмена, троекратное
    превышение — это уже не маржа, а устаревшее или чужое число, и врать про него нельзя.
    """
    mids = [quote.mid for quote in quotes if quote.mid]
    if len(mids) < 2:
        return "single_source", 0.0, None
    low, high = min(mids), max(mids)
    deviation = abs(high - low) / low * 100 if low else 0.0
    official = next((quote.mid for quote in quotes if quote.kind == "official"), None)
    banks = [quote.mid for quote in quotes if quote.kind.startswith("bank") and quote.mid]
    gap = None
    if official and banks:
        gap = abs(sum(banks) / len(banks) - official) / official * 100
    if deviation > tolerance * 3:
        return "conflict", deviation, gap
    if deviation <= tolerance:
        return "agreed", deviation, gap
    return "spread_noted", deviation, gap


async def fetch_rates(
    question: RateQuestion,
    *,
    client: httpx.AsyncClient | None = None,
    cfg: Settings | None = None,
    kv: Any = None,
) -> RateAnswer:
    """Собрать, сверить и закешировать. Кэш — только ускорение: истина всегда в источнике."""
    cfg = cfg or settings()
    key = rates_cache_key(question, cfg)
    use_cache = kv is not None and cfg.rate_cache_ttl_s > 0
    if use_cache:
        cached = await _cache_get(kv, key, question)
        if cached is not None:
            log.info("rates.cache_hit", pair=question.label, age_s=cfg.rate_cache_ttl_s)
            return cached
    answer = await _fetch_uncached(question, client=client, cfg=cfg)
    if use_cache and answer.verdict != "unavailable":
        await _cache_set(kv, key, answer, ttl=cfg.rate_cache_ttl_s)
    return answer


async def _fetch_uncached(
    question: RateQuestion, *, client: httpx.AsyncClient | None, cfg: Settings
) -> RateAnswer:
    now = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
    sources = RateSources(client, cfg=cfg)
    if question.mode == "table":
        codes = list(question.codes or _TABLE_DEFAULT)[:4]
        quotes, causes = await sources.batch(question, codes)
        table: dict[str, list[RateQuote]] = {}
        for quote in quotes:
            table.setdefault(quote.base, []).append(quote)
        if not quotes:
            return RateAnswer(
                question=question,
                verdict="unavailable",
                causes=causes or ["источники не отдали чисел"],
                fetched_at=now,
            )
        return RateAnswer(
            question=question,
            table=table,
            quotes=quotes,
            verdict="agreed",
            fetched_at=now,
            causes=causes,
        )
    quotes, causes = await sources.quotes(question)
    if question.quote.upper() != cfg.rate_home_currency.upper():
        derived = _derive_crosses(quotes, question)
        if derived:
            quotes = derived
        else:
            causes.append(
                f"кросс {question.label}: источники котируют только к "
                f"{cfg.rate_home_currency}, делить не на что"
            )
    if not quotes:
        return RateAnswer(
            question=question,
            verdict="unavailable",
            causes=causes or ["источники не отдали чисел"],
            fetched_at=now,
        )
    verdict, deviation, gap = _verdict(quotes, tolerance=cfg.rate_tolerance_pct)
    answer = RateAnswer(
        question=question,
        quotes=quotes,
        verdict=verdict,
        deviation_pct=deviation,
        official_gap_pct=gap,
        fetched_at=now,
        causes=causes,
    )
    return answer


def _encode(answer: RateAnswer) -> str:
    payload = {
        "question": {
            "base": answer.question.base,
            "quote": answer.question.quote,
            "cash": answer.question.cash,
            "bank": answer.question.bank,
            "mode": answer.question.mode,
            "codes": list(answer.question.codes),
            "raw": answer.question.raw,
        },
        "quotes": [
            {
                "source": q.source,
                "url": q.url,
                "base": q.base,
                "quote": q.quote,
                "buy": q.buy,
                "sell": q.sell,
                "as_of": q.as_of,
                "kind": q.kind,
                "note": q.note,
            }
            for q in answer.quotes
        ],
        "verdict": answer.verdict,
        "deviation_pct": answer.deviation_pct,
        "official_gap_pct": answer.official_gap_pct,
        "table": {code: [q.source for q in quotes] for code, quotes in answer.table.items()},
        "fetched_at": answer.fetched_at,
        "causes": answer.causes,
    }
    return json.dumps(payload, ensure_ascii=False)


def _decode(payload: str | bytes, question: RateQuestion) -> RateAnswer | None:
    try:
        data = json.loads(payload.decode() if isinstance(payload, bytes) else payload)
        quotes = [RateQuote(**row) for row in data.get("quotes", [])]
        meta = data.get("question")
        #: кэш переживает смену вопроса: восстанавливаем вопрос из payload, а не из аргумента
        if isinstance(meta, dict):
            question = RateQuestion(
                base=str(meta.get("base") or question.base),
                quote=str(meta.get("quote") or question.quote),
                cash=meta.get("cash"),
                bank=str(meta.get("bank") or ""),
                mode=str(meta.get("mode") or question.mode),
                codes=tuple(str(code) for code in (meta.get("codes") or ())),
                raw=str(meta.get("raw") or question.raw),
            )
        if not quotes:
            return None
        return RateAnswer(
            question=question,
            quotes=quotes,
            verdict=str(data.get("verdict") or "single_source"),
            deviation_pct=float(data.get("deviation_pct") or 0.0),
            official_gap_pct=data.get("official_gap_pct"),
            fetched_at=str(data.get("fetched_at") or ""),
            from_cache=True,
            causes=list(data.get("causes") or []),
            table=_group(quotes),
        )
    except (ValueError, TypeError) as exc:
        log.warning("rates.cache_invalid", err=repr(exc)[:160])
        return None


def collapse_causes(causes: Sequence[str]) -> list[str]:
    """Одинаковый сбой на трёх источниках — одна строка, а не три.

    Хвост `⚠️` ограничен 400 символами: три копии «ConnectError …» вытесняют из него всё полезное,
    а перечислить, кто именно не ответил, — смысл единственной строчки диагноза.
    """
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for cause in causes:
        name, sep, rest = cause.partition(": ")
        if not sep:
            name, rest = "", cause
        key = rest.strip()
        if not key:
            continue
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        source = name.strip()
        if source and source not in grouped[key]:
            grouped[key].append(source)
    return [(f"{', '.join(grouped[key])}: {key}" if grouped[key] else key) for key in order]


def _group(quotes: list[RateQuote]) -> dict[str, list[RateQuote]]:
    table: dict[str, list[RateQuote]] = {}
    for quote in quotes:
        table.setdefault(quote.base, []).append(quote)
    return table


async def _cache_get(kv: Any, key: str, question: RateQuestion) -> RateAnswer | None:
    """Кэш — только ускорение: при недоступном KV отвечаем по свежему запросу в источники."""
    try:
        raw = await kv.get(key)
    except Exception as exc:  # noqa: BLE001 - состояние KV не должно влиять на ответ
        log.warning("rates.cache_unavailable", op="get", err=repr(exc)[:160])
        return None
    if raw is None:
        return None
    return _decode(raw, question)


async def _cache_set(kv: Any, key: str, answer: RateAnswer, *, ttl: int) -> None:
    try:
        await kv.set(key, _encode(answer), ex=ttl)
    except Exception as exc:  # noqa: BLE001 - не записали кэш — не беда, беда — молчать об этом
        log.warning("rates.cache_unavailable", op="set", err=repr(exc)[:160])


def answer_age_hours(answer: RateAnswer) -> float | None:
    """Сколько часов назад числа были сняты — для «устарело» в интерфейсе."""
    if not answer.fetched_at:
        return None
    try:
        stamp = datetime.fromisoformat(answer.fetched_at)
    except ValueError:
        return None
    return (datetime.now(stamp.tzinfo) - stamp) / timedelta(hours=1)
