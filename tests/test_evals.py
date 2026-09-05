"""Контракт golden-наборов: файл данных должен оставаться пригодным для прогона.

Смысл не в «файл существует», а в трёх рисках, которые убивают evals тихо:

* кейс с неизвестным `kind` не падает громко, а «проходится» — если чекер не зарегистрирован,
  прогон обязан сообщать об этом (и наш тест это фиксирует на самих данных);
* дубликаты `id` превращают «34/34» в «34/34, из них два одинаковых»;
* чекер, который не умеет проваливаться, дороже отсутствия чекера: он создаёт видимость контроля.

Поэтому здесь же — самопроверка: на заведомо неверном ожидании каждый новый чекер обязан вернуть
текст проблемы, а не None.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals"
if str(EVALS) not in sys.path:
    sys.path.insert(0, str(EVALS))

import run_golden  # noqa: E402  (evals — скрипт, а не пакет: путь подвешиваем выше)

SETS = ("golden_v1.jsonl", "golden_v2.jsonl")


def _cases(name: str) -> list[run_golden.Case]:
    return run_golden.load_cases(EVALS / name)


def _raw(name: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in (EVALS / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(json.loads(line))
    return out


@pytest.mark.parametrize("name", SETS)
def test_dataset_parses_and_has_known_kinds(name: str) -> None:
    cases = _cases(name)
    assert cases, f"{name} пуст"
    unknown = sorted({case.kind for case in cases} - set(run_golden.CHECKERS))
    assert not unknown, f"нет чекеров для kinds: {unknown}"


@pytest.mark.parametrize("name", SETS)
def test_ids_are_unique_across_the_set(name: str) -> None:
    ids = [case.id for case in _cases(name)]
    dupes = {item for item in ids if ids.count(item) > 1}
    assert not dupes, f"повторяющиеся id: {sorted(dupes)}"


def test_v2_covers_the_three_families_of_step_two() -> None:
    """v2 — это даты, разбор котировок и сверка чисел; без одной из семей набор не считается.

    Привязка к составу нужна, потому что «золотой набор» легко выродить в набор удобных кейсов:
    тогда он освещает то, что и так работает.
    """
    kinds = [str(raw.get("kind")) for raw in _raw("golden_v2.jsonl")]
    for kind in ("schedule", "rates", "claims", "injection"):
        assert kinds.count(kind) >= 3, f"семейства {kind!r} в v2 меньше трёх кейсов"


@pytest.mark.parametrize("name", SETS)
def test_cases_carry_their_own_expectations(name: str) -> None:
    """Кейс без `expect` = «проверьте, что функция существует»; такой набор ничего не доказывает."""
    for raw in _raw(name):
        kind, cid = str(raw.get("kind")), str(raw.get("id"))
        if kind in ("prompt", "render_chunks", "routing"):
            continue  # у этих форм в v1 собственные ключи ожиданий
        expect = raw.get("expect") or raw.get("expect_contains") or raw.get("expect_absent")
        assert expect, f"{cid} ({kind}): кейс без ожидания"


# ------------------------------------------------------------------ чекеры умеют проваливаться


def test_schedule_checker_can_fail() -> None:
    good = next(case for case in _cases("golden_v2.jsonl") if case.id == "sched-tomorrow-time")
    assert run_golden.check_schedule(good) is None
    wrong = run_golden.Case(
        id="x", kind="schedule", payload={**good.payload, "expect": {"local": "2026-09-06T09:00"}}
    )
    problem = run_golden.check_schedule(wrong)
    assert problem and "local" in problem, "чекер дат не замечает подмены времени"
    refuse = run_golden.Case(
        id="x",
        kind="schedule",
        payload={
            "input": "через 20 минут",
            "now": "2026-09-05T07:00:00",
            "expect": {"refuse": True},
        },
    )
    assert run_golden.check_schedule(refuse)


def test_rates_checker_can_fail() -> None:
    case = next(case for case in _cases("golden_v2.jsonl") if case.id == "rates-nbu-units")
    assert run_golden.check_rates(case) is None
    broken = json.loads(json.dumps(case.payload))
    broken["rows"][1]["units"] = 1  # «уточним» парсер так, что деление на units станет лишним
    problem = run_golden.check_rates(run_golden.Case(id="x", kind="rates", payload=broken))
    assert problem and "buy" in problem, "чекер не заметил, что НБУ-курс перестал делиться на units"


def test_claims_checker_can_fail() -> None:
    case = next(
        case for case in _cases("golden_v2.jsonl") if case.id == "claims-date-without-year-is-fine"
    )
    assert run_golden.check_claims(case) is None
    strict = run_golden.Case(
        id="x",
        kind="claims",
        payload={**case.payload, "expect": {"claims": ["42.00"], "missing": []}},
    )
    assert run_golden.check_claims(strict)


def test_main_reports_each_set_and_exit_code(tmp_path: Path, capsys: Any) -> None:
    """Прогон обязан назвать каждый набор и не «съедать» провал: `make evals` живёт на exit code."""
    assert run_golden.main([]) == 0
    printed = capsys.readouterr().out
    for name in SETS:
        assert Path(name).stem in printed
    broken = tmp_path / "broken.jsonl"
    broken.write_text('{"id":"x","kind":"нет-такого"}\n', encoding="utf-8")
    assert run_golden.main(["--set", str(broken)]) == 1
