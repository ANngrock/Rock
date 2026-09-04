"""Детерминированный путь до модели: он перехватывает только то, чем реально умеет ответить.

Здесь проверяется ровно то, из-за чего путь и появился: «скажи мне курс доллара» обязан получить
число из первоисточника, даже когда LLM нет, бюджет исчерпан и поиск лежит.
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.agents.intents import try_answer
from aegis.platform.config import Settings
from aegis.web.rates import RateAnswer, RateQuestion, RateQuote


def _answer(**over: Any) -> RateAnswer:
    values: dict[str, Any] = {
        "question": RateQuestion(base="USD", quote="UAH", mode="pair", raw="курс доллара"),
        "quotes": [
            RateQuote(
                source="ПриватБанк · безналичный",
                url="https://api.privatbank.ua/x",
                base="USD",
                quote="UAH",
                buy=41.3,
                sell=41.75,
                kind="bank_cashless",
            ),
            RateQuote(
                source="НБУ · официальный",
                url="https://bank.gov.ua/x",
                base="USD",
                quote="UAH",
                buy=41.4,
                sell=41.4,
                kind="official",
            ),
        ],
        "verdict": "agreed",
        "deviation_pct": 0.2,
        "fetched_at": "2026-09-05T01:13:00+03:00",
    }
    values.update(over)
    return RateAnswer(**values)  # type: ignore[arg-type]


@pytest.fixture
def patch_rates(monkeypatch: pytest.MonkeyPatch):
    from aegis.agents import intents

    seen: list[RateQuestion] = []

    def apply(answer: RateAnswer | None) -> list[RateQuestion]:
        async def fake(question: RateQuestion, **_kwargs: Any) -> RateAnswer:
            seen.append(question)
            if answer is None:
                raise AssertionError("не должен был уходить в источники")
            return answer

        monkeypatch.setattr(intents, "fetch_rates", fake)
        return seen

    return apply


_CFG = Settings(_env_file=None, _env_prefix="T_", rate_cache_ttl_s=0, rate_home_currency="UAH")


async def test_rate_question_is_answered_from_the_source(patch_rates: Any) -> None:
    seen = patch_rates(_answer())
    answer = await try_answer("скажи мне актуальный курс доллара в приватбанке", cfg=_CFG)
    assert answer is not None and answer.intent == "rates"
    assert seen and seen[0].base == "USD"
    assert "41." in answer.text and "НБУ" in answer.text
    meta = answer.meta
    assert meta["verdict"] == "agreed" and meta["question"] == "USD/UAH"
    assert meta["sources"], "воспроизводимость: чем подтверждали — видно в трассе"


async def test_not_a_rate_question_is_left_to_the_model(patch_rates: Any) -> None:
    patch_rates(None)  # если дойдёт до источников — тест упадёт
    for text in ("привет", "запомни: рост 182", "столица германии", "сколько будет 2+2"):
        assert await try_answer(text, cfg=_CFG) is None, text


async def test_dead_sources_fall_through_with_the_reason(patch_rates: Any) -> None:
    notices: list[str] = []
    patch_rates(
        _answer(verdict="unavailable", quotes=[], causes=["privatbank: ConnectError", "nbu: 502"])
    )
    answer = await try_answer("курс доллара", cfg=_CFG, notices=notices)
    assert answer is None, "без чисел мы не выдумываем ответ — отдаём ход поиску и модели"
    assert notices and "privatbank" in notices[0] and "nbu" in notices[0]


async def test_attachments_are_not_hijacked(patch_rates: Any) -> None:
    """На фото чека «какой курс» отвечает vision-путь, а не парсер текста."""
    patch_rates(_answer())
    answer = await try_answer("", cfg=_CFG)
    assert answer is None
