"""Сверка ответа с источниками (шаг 2): детерминированная часть + судья в чистом контексте.

Главное, что здесь проверяется, — не «функция вернула список», а три обещания владельцу:
число из воздуха не уходит в ответ молча; отказ судьи не отменяет ответ; судья не видит ни истории,
ни инструментов (иначе «проверка» была бы ещё одним способом протащить чужую инструкцию).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from aegis.agents.verify import Verdict, Verifier, VerifyJudgement, extract_claims, find_unverified
from aegis.platform.config import Settings
from aegis.platform.gateway.cost import CostGovernor
from conftest import FakeGateway, FakeKV

SOURCE = "NBU: 43.18 UAH за доллар на 2026-09-03. Вчера было 42,90."


def _settings(**over: Any) -> Settings:
    """Порог длины выключен: эти тесты про логику, а не про «отвечать коротко нельзя»."""
    over.setdefault("verify_min_answer_chars", 0)
    return Settings(_env_file=None, _env_prefix="V_", glm_api_key="k", **over)


def _verifier(*, judgements: list[Any] | None = None, **over: Any) -> tuple[Verifier, FakeGateway]:
    cost = CostGovernor(FakeKV(), daily_limit_usd=5.0)  # type: ignore[arg-type]
    gateway = FakeGateway([], cost, judgements=judgements)
    return Verifier(gateway=gateway, cfg=_settings(**over)), gateway  # type: ignore[arg-type]


# ------------------------------------------------------------------ что считаем утверждением


def test_numbers_and_dates_are_read_in_any_spelling() -> None:
    claims = extract_claims(
        "Курс 43,18 ₴ (вчера 42.9 и 1 234,56 где-то ещё). Данные на 03.09.2026, "
        "встреча 5 сентября 2026."
    )
    assert "43.18" in claims
    assert "42.9" in claims  # точка в конце предложения — не часть числа и не преграда
    assert "1234.56" in claims
    assert "03.09.2026" in claims
    assert "05.09.2026" in claims
    # дату нельзя расчленить на «числа»: иначе проверка крысила бы саму себя
    assert "09.2026" not in claims and "03.09" not in claims


def test_structural_numbers_are_not_claims() -> None:
    assert extract_claims("2 варианта, 1 идея, ок") == ()
    assert extract_claims("в 2026 году всё было иначе") == ()
    assert extract_claims("пункт 3.2 списка") == ("3.2",)


def test_comparison_ignores_the_form_of_the_number() -> None:
    claims = extract_claims("Итого 1 234,50 ₽")
    assert claims == ("1234.5",)
    assert find_unverified(claims, ["Итого 1234.500 ₽"]) == []
    assert find_unverified(claims, ["Итого 1235,50 ₽"]) == ["1234.5"]


def test_date_without_year_in_source_is_not_a_discrepancy() -> None:
    """Источник мог не повторить год: «3 сентября» и «03.09.2026» — одна дата."""
    assert find_unverified(["03.09.2026"], ["данные за 3 сентября"]) == []
    assert find_unverified(["03.09.2026"], ["данные за 4 сентября"]) == ["03.09.2026"]


# ------------------------------------------------------------------ решение «проверять ли»


def test_should_verify_needs_a_claim_and_a_source() -> None:
    """Сверяем, когда есть что сверять (числа/даты) и чем (внешние результаты)."""
    verifier, _ = _verifier()
    assert verifier.should_verify(answer="Курс 43,18 ₴ на 03.09.2026", sources=[SOURCE]) is True
    assert (
        verifier.should_verify(answer="Ничего числового в этом ответе нет", sources=[SOURCE])
        is False
    )
    assert verifier.should_verify(answer="Курс 12,34 ₴", sources=[]) is False


def test_short_answers_are_not_checked_and_disabled_is_disabled() -> None:
    verifier, _ = _verifier(verify_min_answer_chars=500)
    assert verifier.should_verify(answer="Курс 12,34 ₴", sources=[SOURCE]) is False
    off, _ = _verifier(verify_enabled=False)
    assert off.should_verify(answer="Курс 12,34 ₴ и ещё 99,99 ₽", sources=[SOURCE]) is False


async def test_verify_always_calls_the_judge_even_when_numbers_match() -> None:
    verifier, gateway = _verifier(verify_always=True, judgements=[{"consistent": True}])
    assert verifier.should_verify(answer="Курс 43,18 ₴", sources=[SOURCE]) is True
    assert gateway.json_calls == []  # should_verify не имеет права тратить вызов
    verdict = await verifier.verify(question="q", answer="Курс 43,18 ₴", sources=[SOURCE])
    assert verdict.ok and verdict.judged and len(gateway.json_calls) == 1


# ------------------------------------------------------------------ сама сверка


async def test_unsupported_number_becomes_a_critical_problem() -> None:
    verifier, _ = _verifier()
    verdict = await verifier.verify(
        question="какой курс и что платить?",
        answer="Курс 43,18 ₴, к оплате 12 345,00 ₽ завтра.",
        sources=[SOURCE],
    )
    assert not verdict.ok
    assert verdict.severity == "critical"  # деньги + неподтверждённое число
    assert verdict.mode == "unavailable"  # скрипта судьи нет → это видно, а не «всё чисто»
    assert any("12345" in item for item in verdict.problems)
    notice = verdict.notice()
    assert notice and notice.startswith("ответ не подтверждён источниками")
    assert "судья недоступен" in notice


async def test_everything_supported_leaves_the_answer_alone() -> None:
    verifier, _ = _verifier()
    verdict = await verifier.verify(
        question="kurs?", answer="Курс 43,18 ₴ на 03.09.2026.", sources=[SOURCE]
    )  # судьи нет
    assert verdict.ok and verdict.problems == () and verdict.notice() is None
    # «проверено целиком» без судьи сказать нельзя: детерминированная часть — только половина
    assert verdict.severity == "unavailable" and verdict.mode == "unavailable"


async def test_clean_numbers_and_a_thankful_judge_mean_no_secrets() -> None:
    verifier, _ = _verifier(judgements=[{"consistent": True, "severity": "none"}])
    verdict = await verifier.verify(question="kurs?", answer="Курс 43,18 ₴.", sources=[SOURCE])
    assert verdict.ok and verdict.severity == "none" and verdict.judged


async def test_judge_can_flag_more_than_numbers() -> None:
    verifier, gateway = _verifier(
        judgements=[
            {
                "consistent": False,
                "severity": "minor",
                "unsupported": ["«НБУ отменил публикацию курсов»"],
                "corrections": ["скажи, что в источниках этого нет"],
            }
        ]
    )
    verdict = await verifier.verify(
        question="курс?",
        answer="Курс 43,18 ₴. НБУ отменил публикацию курсов.",
        sources=[SOURCE],
        trace_id="t",
    )
    assert verdict.judged and verdict.mode == "judged"
    assert not verdict.ok
    assert any("НБУ отменил" in item for item in verdict.problems)
    assert any(item.startswith("уточнение:") for item in verdict.problems)
    assert verdict.severity == "minor"

    (call,) = gateway.json_calls
    assert call["schema"] == "VerifyJudgement"
    assert call["role"] == "fast"
    assert call["thinking"] is False


async def test_the_judge_sees_a_clean_context_only() -> None:
    """Ни истории, ни инструментов, ни системного промпта хода — иначе «проверка» была бы ещё
    одной дверью для чужих инструкций."""
    verifier, gateway = _verifier(judgements=[{"consistent": False, "severity": "critical"}])
    await verifier.verify(question="q", answer="Курс 99 ₴", sources=[SOURCE])
    (call,) = gateway.json_calls
    roles = [msg["role"] for msg in call["messages"]]
    assert roles == ["system", "user"]
    body = call["messages"][0]["content"]
    assert "ИСТОЧНИКИ" in body and "43.18" in body and "99" in body


async def test_agreement_of_the_judge_closes_minor_findings() -> None:
    verifier, _ = _verifier(
        verify_always=True, judgements=[{"consistent": True, "severity": "none"}]
    )
    verdict = await verifier.verify(question="q", answer="Курс 43,18 ₴", sources=[SOURCE])
    assert verdict.ok and verdict.judged and verdict.problems == ()


async def test_broken_judge_never_swallows_the_deterministic_finding() -> None:
    verifier, _ = _verifier(judgements=[RuntimeError("502 от провайдера")])
    verdict = await verifier.verify(question="q", answer="Выплата 777 ₽", sources=[SOURCE])
    assert not verdict.ok and not verdict.judged
    assert verdict.note == "судья недоступен"
    assert any("777" in item for item in verdict.problems)


async def test_verdict_carries_the_prompt_it_was_judged_by() -> None:
    """Вердикт без критерия — то же, что ответ без источника: через год не понять, чем мерили."""
    verifier, _ = _verifier(judgements=[{"consistent": True}])
    verdict = await verifier.verify(question="q", answer="Курс 12,34 ₴", sources=[SOURCE])
    assert verdict.judged
    (ref,) = verdict.prompt_ids
    assert ref["id"] == "verify/judge" and len(ref["sha256"]) == 64


def test_long_problems_are_capped_for_the_owner() -> None:
    verdict = Verdict(
        ok=False,
        problems=tuple(f"число {index} не подтверждено" for index in range(10)),
        judged=True,
    )
    notice = verdict.notice()
    assert notice and len(notice) <= 400 and "и ещё 7" in notice


def test_judgement_shape_is_strict() -> None:
    """Схема судьи — контракт: «severity» не принимает что попало, «consistent» обязателен."""
    with pytest.raises(ValidationError):
        VerifyJudgement.model_validate({"consistent": True, "severity": "катастрофа"})
    with pytest.raises(ValidationError):
        VerifyJudgement.model_validate({"severity": "minor"})
