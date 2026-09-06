"""Юнит-контракты операционных слоёв: флаги (F6), «один ход на владельца» (F1), saga (F8).

Базы здесь нет специально: это чистые функции решения — «какой bucket у actor'а», «пустит ли
allow-list», «в каком порядке компенсировать». Всё, что требует Postgres, — в
``tests/integration/test_turns_concurrency.py``; разделение честное: если логика спорит с
порядком и кэшем, ей место в юните, если с блокировками — в интеграции.
"""

from __future__ import annotations

from typing import Any

from aegis.governance.turns import NullTurnLedger
from aegis.platform.flags import FLAG_CATALOG, FlagEngine, bucket_for
from aegis.workflows.dual import compare_trace, summarize
from aegis.workflows.ledger import NullLedger, run_once
from aegis.workflows.plan import PlannedStep, step_id
from aegis.workflows.turn import Compensation, run_turn_saga


class FakeSource:
    def __init__(
        self, rows: dict[str, dict[str, Any]] | None = None, *, boom: bool = False
    ) -> None:
        self._rows = rows or {}
        self._boom = boom

    async def load(self) -> dict[str, dict[str, Any]]:
        if self._boom:
            raise ConnectionError("nats… то есть redis лёг")
        return self._rows


# ------------------------------------------------------------------ флаги (F6)


async def test_allow_and_deny_beat_percentile() -> None:
    engine = FlagEngine(
        FakeSource(
            {
                "notes.hybrid_search": {
                    "percent": 0,
                    "allow": [7],
                    "deny": [9],
                    "stage": "canary",
                }
            }
        )
    )
    on = await engine.evaluate("notes.hybrid_search", 7)
    assert on.on and on.basis == "allowlist"
    off = await engine.evaluate("notes.hybrid_search", 9)
    assert not off.on and off.basis == "denylist", (
        "deny проверяется раньше allow — иначе список «для отвода глаз»"
    )
    plain = await engine.evaluate("notes.hybrid_search", 11)
    assert not plain.on and plain.basis == "off"


async def test_full_and_off_and_unknown() -> None:
    engine = FlagEngine(
        FakeSource(
            {
                "agent.verify_answers": {"percent": 100, "allow": [], "deny": []},
                "telegram.stream_replies": {"percent": 0, "allow": [], "deny": []},
            }
        )
    )
    assert (await engine.evaluate("agent.verify_answers", 1)).basis == "full"
    assert (await engine.evaluate("telegram.stream_replies", 1)).on is False
    missing = await engine.evaluate("совершенно.неизвестный", 1)
    assert not missing.on and missing.basis == "default"


async def test_bucket_is_deterministic_and_key_scoped() -> None:
    first = bucket_for("notes.hybrid_search", 42)
    again = bucket_for("notes.hybrid_search", 42)
    assert first == again, (
        "тот же (flag, actor) обязан давать тот же bucket — иначе A/B не пересчитать"
    )
    assert all(0 <= bucket_for(k, i) < 100 for k in FLAG_CATALOG for i in range(50))
    other_key = {bucket_for("agent.verify_answers", i) for i in range(200)}
    assert len(other_key) > 50, "bucket по разным флагам не должен быть «одна и та же решётка»"


async def test_percentile_selects_the_bucket_not_the_mood() -> None:
    engine = FlagEngine(
        FakeSource({"notes.hybrid_search": {"percent": 50, "allow": [], "deny": []}})
    )
    decisions = {i: await engine.evaluate("notes.hybrid_search", i) for i in range(300)}
    share = sum(1 for d in decisions.values() if d.on) / len(decisions)
    assert 0.40 < share < 0.60, f"p≈50 обязан попадать в коридор, попало {share:.2f}"
    for actor, decision in decisions.items():
        assert decision.on == (bucket_for("notes.hybrid_search", actor) < 50)
        assert decision.basis == "bucket"


async def test_snapshot_covers_catalog_and_records_basis() -> None:
    engine = FlagEngine(
        FakeSource({"telegram.stream_replies": {"percent": 100, "allow": [], "deny": []}})
    )
    snap = await engine.snapshot(3)
    assert set(snap) == set(FLAG_CATALOG)
    jv = snap["telegram.stream_replies"].journal_value()
    assert jv["on"] is True and jv["basis"] == "full", (
        "в decision_records попадает форма для пересчёта: флаг без source-истории не воспроизводим"
    )
    assert set(jv) == {"on", "basis", "src"}


# ------------------------------------------------------------------ ходы (F1): null-контракт


async def test_null_ledger_open_advance_close() -> None:
    ledger = NullTurnLedger()
    assert ledger.durable is False
    handle = await ledger.begin("trace-1", owner_id=1)
    assert handle.stale(), "null-ход обязан объявлять себя волатильным — это контракт, не деталь"
    assert await ledger.advance("trace-1", handle.fencing_token) == 1
    assert await ledger.advance("trace-1", handle.fencing_token) == 2
    assert await ledger.finish("trace-1", handle.fencing_token) is True
    assert await ledger.finish("trace-1", handle.fencing_token) is False, (
        "второй finish — уже не факт"
    )
    assert (await ledger.status())["active"] == 0


async def test_null_ledger_queue_round_trip() -> None:
    ledger = NullTurnLedger()
    for i in range(3):
        await ledger.enqueue(1, trace_id=f"t{i}", kind="message", payload={"n": i})
    assert await ledger.queue_count(1) == 3
    popped = await ledger.pop_next(1)
    assert popped is not None and popped.payload["n"] == 0, (
        "очередь — FIFO: «второе сообщение» не перепутается с первым"
    )
    assert await ledger.pop_next(2) is None, (
        "чужому владельцу очередь не показывается даже в памяти"
    )


# ------------------------------------------------------------------ реестр активностей (F8)


async def test_null_ledger_dedups_in_process_and_declares_it_not_durable() -> None:
    """Null-реестр дедуплицирует «до рестарта» — и обязан это объявлять (durable=False).

    Профиль памяти честен ровно на свой радиус: повтор в процессе получает сохранённый
    результат, а не второй эффект; «пережил ли это рестарт» — то, за что отвечает SQL-версия.
    """
    calls = 0

    async def effect() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    ledger = NullLedger()
    await run_once(ledger, "t:1:tool:pay", effect)
    b = await run_once(ledger, "t:1:tool:pay", effect)
    assert calls == 1, "второй вызов того же activity_id не имеет права платить дважды"
    assert b.replayed and b.result == {"calls": 1}, "повтор = сохранённый результат, не пересчёт"
    assert ledger.durable is False


def test_step_id_is_the_canonical_contract() -> None:
    assert step_id("abcd", 3, "tool", "pay") == "abcd:3:tool:pay"
    assert step_id("abcd", 3, "turn") == "abcd:3:turn"


# ------------------------------------------------------------------ saga + dual-run (F8)


async def test_saga_compensates_in_reverse_order_and_stops() -> None:
    trace = "11111111-2222-3333-4444-555555555555"
    log: list[str] = []

    async def executor(step: PlannedStep) -> dict[str, Any]:
        if step.tool == "boom":
            raise RuntimeError("внешний мир подвёл")
        log.append(f"do:{step.tool}")
        return {"tool": step.tool}

    async def undo(step: PlannedStep, result: dict[str, Any]) -> str:
        log.append(f"undo:{result.get('tool')}")
        return "отменено"

    steps = [
        PlannedStep(seq=1, kind="tool", tool="first"),
        PlannedStep(seq=2, kind="tool", tool="second"),
        PlannedStep(seq=3, kind="tool", tool="boom"),
    ]
    compensations = {"tool": Compensation(kind="tool", compensator=undo)}
    result = await run_turn_saga(trace, steps, executor=executor, compensations=compensations)
    assert not result.ok
    assert log == ["do:first", "do:second", "undo:second", "undo:first"], (
        "saga: компенсация строго в обратном порядке и строго уже сделанного"
    )


async def test_saga_replay_skips_done_steps() -> None:
    """Повтор того же плана (крах процесса между шагами) исполняет только незакрытое."""
    trace = "11111111-2222-3333-4444-555555555555"
    executed: list[str] = []

    async def executor(step: PlannedStep) -> dict[str, Any]:
        executed.append(step.tool)
        return {"tool": step.tool}

    steps = [
        PlannedStep(seq=1, kind="tool", tool="pay"),
        PlannedStep(seq=2, kind="tool", tool="note"),
    ]
    ledger = NullLedger()  # тот же «done помнится в процессе», что и после краха до SQL-словаря
    await run_turn_saga(trace, steps, executor=executor, ledger=ledger)
    result = await run_turn_saga(trace, steps, executor=executor, ledger=ledger)
    assert executed == ["pay", "note"], "второй прогон не платит дважды"
    assert result.replayed == 2


def test_dual_run_comparison_surfaces_step_divergence() -> None:
    trace = "abc"
    rows = [
        {
            "id": 1,
            "kind": "turn",
            "turn_no": 1,
            "params": {},
        },  # контекст, не шаг — обязан игнорироваться
        {"id": 2, "kind": "policy", "turn_no": 1, "params": {}},
        {"id": 3, "kind": "tool_run", "turn_no": 2, "params": {"tool": "web_search"}},
    ]
    same = compare_trace(trace, rows)
    assert same.clean and same.matched == 2, "детерминатор обязан узнавать собственный след"
    shuffled = [rows[2], rows[1]]  # журнал утверждает, что tool_run шёл раньше policy того же хода
    drift = compare_trace(trace, [dict(r, turn_no=1) for r in shuffled])
    assert not drift.clean and drift.order_mismatch, (
        "порядок внутри хода — часть следа: исполнитель сверяется с ним, а не с пожеланиями"
    )
    report = summarize([same, drift])
    assert report["diverged"] == 1 and report["traces"] == 2


# ------------------------------------------------------------------ SLO-артефакт (F7)


def test_committed_alerts_artifact_matches_slo_file() -> None:
    """``deploy/slo.alerts.yml`` — не «копия, актуальная когда повезёт», а проверяемый артефакт.

    Прометей-алерты живут отдельным файлом ровно потому, что их читает не наш код, а Alertmanager;
    расхождение «правила в slo.yml уже такие, а в проде старые» здесь превращается в красный
    тест, а не в «сигнализации нет, но мы не заметили».
    """
    from pathlib import Path  # noqa: PLC0415

    from aegis.platform.slo import load_slo_file, render_alerts  # noqa: PLC0415

    root = Path(__file__).resolve().parents[1]
    rendered = render_alerts(load_slo_file(root / "deploy" / "slo.yml"))
    committed = (root / "deploy" / "slo.alerts.yml").read_text(encoding="utf-8")
    assert committed.strip() == rendered.strip(), (
        "артефакт устарел: aegis slo alerts --write deploy/slo.alerts.yml"
    )
