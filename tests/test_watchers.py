"""Наблюдатели офлайн: арифметика вердиктов и решений.

SQL — в интеграциях; здесь то, что портит смысл фичи без единой строчки БД: первый проход
'changed' не обязан стрелять, one-shot гаснет после попадания, десять падений — пауза, а
фрагмент-доказательство не имеет права протащить в текст владельца управляющие последовательности.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aegis.planning.watchers import (
    Watch,
    WatchReport,
    decide_after_check,
    evaluate,
    excerpt,
    fire_text,
    normalize_needle,
    watch_failures_left,
)

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _watch(**kw: object) -> Watch:
    base: dict = dict(
        id="a1b2c3d4-0000-0000-0000-000000000000",
        owner_id=1,
        title="цена билета",
        kind="page",
        target="https://example.test/tickets",
        mode="contains",
        needle="3 400",
        interval_minutes=15,
        channel="message",
        repeat=False,
        status="active",
        fire_at=NOW,
        expires_at=None,
        baseline_hash=None,
        failures=0,
    )
    base.update(kw)
    return Watch(**base)  # type: ignore[arg-type]


class TestEvaluate:
    def test_contains_case_insensitive(self) -> None:
        hit, _digest, proof = evaluate("contains", "МИР", "погода: мир и тишина", None)
        assert hit and "найдено" in str(proof)

    def test_regex(self) -> None:
        hit, _, proof = evaluate("regex", r"\d+ \w*", "цена 3 400 рублей", None)
        assert hit and "совпадение" in str(proof)
        assert evaluate("regex", r"\d{9}", "нет длинных чисел", None)[0] is False

    def test_changed_seeds_baseline_without_firing(self) -> None:
        """Первый проход 'changed' — только эталон. Иначе любое наблюдение стреляет в момент
        создания, а «изменение» для владельца означает «что-то поменялось», а не «я стартовал»."""
        hit, digest, _ = evaluate("changed", None, "содержимое", None)
        assert hit is False and digest
        hit2, _, proof = evaluate("changed", None, "содержимое", digest)
        assert hit2 is False and proof is None
        hit3, _, proof3 = evaluate("changed", None, "новое содержимое", digest)
        assert hit3 is True and "изменилось" in str(proof3)


class TestGuards:
    def test_needle_required_except_changed(self) -> None:
        with pytest.raises(ValueError, match="требует условие"):
            normalize_needle("contains", "   ")

    def test_broken_regex_rejected_at_creation(self) -> None:
        # regex, падающий в тике, — невидимый баг: проверка обязана быть при постановке
        with pytest.raises(ValueError, match="регулярное выражение"):
            normalize_needle("regex", "3(400")

    def test_changed_ignores_needle(self) -> None:
        assert normalize_needle("changed", "что угодно") is None


class TestDecisions:
    def test_one_shot_hit_is_final(self) -> None:
        assert (
            decide_after_check(True, now=NOW, interval_minutes=15, expires_at=None, repeat=False)
            == "fired"
        )

    def test_repeat_hit_rearms(self) -> None:
        assert (
            decide_after_check(True, now=NOW, interval_minutes=15, expires_at=None, repeat=True)
            == "rearm"
        )

    def test_horizon_checked_on_next_fire_not_now(self) -> None:
        expires = NOW + timedelta(minutes=10)  # интервал 15 — следующий заход уже за горизонтом
        assert (
            decide_after_check(
                False, now=NOW, interval_minutes=15, expires_at=expires, repeat=False
            )
            == "expired"
        )

    def test_failures_countdown(self) -> None:
        assert watch_failures_left(_watch(failures=9)) == 1
        assert watch_failures_left(_watch(failures=10)) == 0


class TestExcerptAndText:
    def test_excerpt_centers_on_needle(self) -> None:
        body = "а" * 500 + " 3 400 рублей " + "б" * 500
        frag = excerpt(body, "3 400", width=60)
        assert "3 400" in frag and len(frag) < 80

    def test_excerpt_without_needle_shows_tail(self) -> None:
        body = "старое начало… " + "новая строка внизу"
        assert "новая строка внизу" in excerpt(body)

    def test_fire_text_carries_proof(self) -> None:
        text = fire_text(_watch(), "найдено «3 400»", "…цена 3 400 рублей…")
        assert text.startswith("👁 цена билета") and "найдено" in text and "3 400" in text

    def test_report_summary_counts(self) -> None:
        report = WatchReport(checked=3, fired=1, rearmed=2, notes=["x"])
        assert "сработало 1" in report.summary()
        assert report.counts()["rearmed"] == 2
