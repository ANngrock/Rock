"""Сага хода (F8): последовательность активностей + компенсации + один детерминатор на все режимы.

Правила, за которые здесь платят кодом:

* **исполнил ровно один раз.** :class:`ActivityOutcome` с ``replayed=True`` означает «эффект уже
  есть в ledger» — обработчик НЕ вызывается; идемпотентность держится на id активности, а не на
  честном слове handler'а (handler может быть «не дважды, а один раз» только вместе с реестром);
* **необратимое — компенсируется, а не отменяется.** ``send`` уже ушёл в Telegram — откатить нельзя;
  можно записать компенсирующее действие (``compensations``), и раннер делает это автоматически для
  шагов с зарегистрированной компенсацией («отправил → предупредили об отзыве»);
* **один детерминатор.** ``turn_workflow`` строит шаги через :func:`aegis.workflows.plan` и его же
  использует Temporal-обёртка: если кластера нет, тот же код идёт локально (ADR-0021: «нет
  Temporal — норма», а не «нет Temporal — простой»).

Раннер не знает про модели и Telegram: executor и compensator приходят аргументами — ровно так,
как это нужно и для Temporal-activity, и для unit-тестов.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from aegis.workflows.ledger import Ledger, NullLedger
from aegis.workflows.plan import CODE_VERSION, PlannedStep, step_id

__all__ = [
    "Compensator",
    "Compensation",
    "SagaResult",
    "TurnExecutor",
    "run_turn_saga",
    "steps_from_dicts",
]

log = structlog.get_logger(__name__)

#: исполняет шаг; возвращает сериализуемый результат (он попадёт в ledger как доказательство)
TurnExecutor = Callable[[PlannedStep], Awaitable[dict[str, Any]]]
#: компенсация исполненного шага (например, «ответ ушёл, но план сломался — предупреди»)
Compensator = Callable[[PlannedStep, dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class Compensation:
    kind: str
    compensator: Compensator


@dataclass(slots=True)
class SagaResult:
    """Итог прогона: что исполнили, что взяли из реестра, чем compensate'или."""

    code_version: str = CODE_VERSION
    executed: int = 0
    replayed: int = 0
    skipped_in_flight: int = 0
    compensated: list[str] = field(default_factory=list)
    failed: str = ""
    results: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        bits = [
            f"{self.code_version}: исполнено {self.executed}",
            f"из реестра {self.replayed}",
        ]
        if self.skipped_in_flight:
            bits.append(f"в работе у других {self.skipped_in_flight}")
        if self.compensated:
            bits.append(f"компенсировано {len(self.compensated)}: " + ", ".join(self.compensated))
        if self.failed:
            bits.append(f"⚠️ {self.failed}")
        return " · ".join(bits)


async def run_turn_saga(
    trace_id: str,
    steps: Sequence[PlannedStep],
    *,
    executor: TurnExecutor,
    ledger: Ledger | None = None,
    compensations: Mapping[str, Compensation] | None = None,
    fencing_token: int = 0,
) -> SagaResult:
    """Прогнать план через реестр. Исключение шага = остановка + компенсация уже сделанного.

    Компенсации применяются в обратном порядке (classical saga): «последний необратимый шаг»
    должен быть «закрыт» первым. Компенсация не умеет быть необратимой сама: её отказ логируется и
    возвращается в ``failed`` — «почини руками» честнее «мы сделали вид, что откатили».
    """
    store: Ledger = ledger or NullLedger()
    registry = dict(compensations or {})
    result = SagaResult()
    #: (activity_id, шаг, результат) — id нужен компенсатору, чтобы пометить ровно ту запись
    completed: list[tuple[str, PlannedStep, dict[str, Any]]] = []
    for step in steps:
        aid = step_id(trace_id, step.seq, step.kind, step.tool)
        state = await store.start(aid, fencing_token=fencing_token, trace_id=trace_id)
        if state is not None:
            if state.state == "done" or state.state == "compensated":
                result.results[aid] = dict(state.result or {})
                result.replayed += 1
                continue
            if state.state == "running":
                # чужой живой исполнитель: ждать здесь нечего (ход владельца идёт своим путём),
                # «пропустили, потому что уже делают» — отдельный счётчик, а не ошибка
                result.skipped_in_flight += 1
                continue
            if state.state == "failed":
                # «уже падал» ≠ «не исполнять снова»: fail — терминальное состояние для этой
                # попытки, и шаг исполняется заново тем же id (attempts растёт в ledger).
                # Пропускать здесь значило бы «остановиться навсегда на одной неудаче».
                pass
        try:
            payload = await executor(step)
        except Exception as exc:  # noqa: BLE001 — провал шага переводится в сагу, не в traceback вызывающему
            await store.fail(aid, f"{type(exc).__name__}: {exc}", fencing_token=fencing_token)
            result.failed = f"{step.kind}:{step.tool} — {type(exc).__name__}: {exc}"[:300]
            await _compensate(store, registry, completed, result)
            return result
        await store.complete(aid, dict(payload), fencing_token=fencing_token)
        result.results[aid] = dict(payload)
        result.executed += 1
        completed.append((aid, step, dict(payload)))
    return result


async def _compensate(
    store: Ledger,
    registry: Mapping[str, Compensation],
    completed: Sequence[tuple[str, PlannedStep, dict[str, Any]]],
    result: SagaResult,
) -> None:
    for aid, step, payload in reversed(list(completed)):
        comp = registry.get(step.kind)
        if comp is None:
            continue
        try:
            note = await comp.compensator(step, payload)
        except Exception as exc:  # noqa: BLE001 — отказ компенсации обязан быть виден, а не проглочен
            result.failed += f" | компенсация {step.kind} не удалась: {type(exc).__name__}"[:200]
            log.error("saga.compensation_failed", kind=step.kind, err=repr(exc)[:200])
            return
        await store.compensate(aid, note)
        result.compensated.append(f"{step.kind}:{step.tool}" if step.tool else step.kind)


def steps_from_dicts(rows: Sequence[Mapping[str, Any]]) -> list[PlannedStep]:
    """Для CLI dual-run: dict → PlannedStep (без импорта plan.plan_from_journal ради мелочи)."""
    return [
        PlannedStep(
            seq=int(r.get("seq") or 0),
            kind=str(r.get("kind") or "reply"),  # type: ignore[arg-type]
            tool=str(r.get("tool") or ""),
            ok=bool(r.get("ok", True)),
            decision=str(r.get("decision") or ""),
        )
        for r in rows
    ]
