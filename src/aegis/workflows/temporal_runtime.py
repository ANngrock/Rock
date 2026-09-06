"""Temporal-адаптер воркфлоу (F8): опциональная реализация, а не обязательная зависимость.

Правило ADR-0021: отсутствие кластера — норма. Поэтому:

* ``temporalio`` импортируется лениво: без пакета модуль импортируется, приложение работает,
  doctor объясняет, что включено, а чего нет;
* воркфлоу использует ТОТ ЖЕ детерминатор шагов и ТОТ ЖЕ ledger, что и локальный раннер:
  два режима расходятся транспортом исполнения, а не поведением — иначе «мы сверяли dual-run»
  ничего не значит;
* ``workflow.patched`` завязан на :data:`aegis.workflows.plan.PATCHES`: смена поведения не может
  молча переписать идущие экземпляры (долгие reminder-цепочки пережили бы деплой, а мы бы
  думали, что «это же один код»);
* каждый шаг — activity с ``id=step_id(...)``: Temporal сам схлопывает повторные запуски шага
  в один (idempotent activity id) плюс наш ledger как второй замок — для краха между
  «activity ack'нулась» и «workflow продолжился».

Здесь нет test-ов на живой Temporal: их нечем гонять без кластера, и мы это не прячем (см. ADR).
Логику, которая проверяется офлайн, держат plan/ledger/turn.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from aegis.workflows.plan import CODE_VERSION, PATCHES, PlannedStep, step_id

__all__ = [
    "TemporalUnavailable",
    "WorkflowSettings",
    "activity_options",
    "import_runtime",
    "worker_payload",
]

log = structlog.get_logger(__name__)


class TemporalUnavailable(RuntimeError):
    """Кластер/пакет недоступен. НЕ авария бота: раннер остаётся локальным."""


@dataclass(frozen=True, slots=True)
class WorkflowSettings:
    task_queue: str = "aegis-turn"
    start_to_close_s: float = 120.0
    retries_per_activity: int = 3
    code_version: str = CODE_VERSION


def import_runtime() -> Any:
    """Импорт temporalio.workflow или исключение с инструкцией (тот же контракт, что у
    nats-адаптера)."""
    try:
        from temporalio import workflow as temporal_workflow  # noqa: PLC0415
    except ImportError as exc:
        raise TemporalUnavailable('нет пакета temporalio: pip install -e ".[durable]"') from exc
    return temporal_workflow


def activity_options(
    settings: WorkflowSettings, trace_id: str, step: PlannedStep
) -> dict[str, Any]:
    """Параметры execute_activity. Чистая функция — её же сверяет unit-тест."""
    return {
        "id": step_id(trace_id, step.seq, step.kind, step.tool),
        "start_to_close_timeout": settings.start_to_close_s,
        "retry_policy": {
            "initial_interval": 1.0,
            "backoff_coefficient": 2.0,
            "maximum_attempts": settings.retries_per_activity,
        },
    }


def patched_flags() -> tuple[str, ...]:
    """Точки патча для воркфлоу: каждый — условие ``workflow.patched(name)`` внутри тела."""
    return PATCHES


def worker_payload(
    settings: WorkflowSettings, activities: Mapping[str, Any], workflows: Sequence[str]
) -> dict[str, Any]:
    """Что пойдёт в Worker(...) — данные, чтобы конфигурацию можно было сравнить в тесте."""
    return {
        "task_queue": settings.task_queue,
        "activities": {name: spec for name, spec in activities.items()},
        "workflows": list(workflows),
    }


def build_turn_workflow_class() -> Any:
    """Собрать класс воркфлоу при наличии temporalio. Без пакета — TemporalUnavailable.

    Отдельная функция (а не аннотатор на import модуля) — потому что декораторы ``@workflow.defn``
    вычисляются при импорте модуля: модуль, который бот импортирует всегда, не имеет права
    падать из-за отсутствия факультативного кластера.
    """
    temporal_workflow = import_runtime()
    from temporalio import activity as temporal_activity  # noqa: PLC0415

    @temporal_workflow.defn(name="AgentTurnWorkflow")
    class AgentTurnWorkflow:
        """Ход агента как воркфлоу. Тело маленькое осознанно: ветвление — у детерминатора.

        Шаги берутся из входа (plan), activity id — из step_id: «payment не уйдёт дважды»
        обеспечивает сам Temporal (повтор activity-id → уже завершённая activity возвращает
        сохранённый результат), а ledger закрывает окно между activity ack и commit'ом домена.
        """

        def __init__(self) -> None:
            self._compensation_sent = False

        @temporal_workflow.run()
        async def run(self, plan: list[dict[str, Any]], settings: dict[str, Any]) -> dict[str, Any]:
            cfg = WorkflowSettings(**settings) if settings else WorkflowSettings()
            results: dict[str, Any] = {}
            steps = [
                PlannedStep(
                    seq=int(row.get("seq") or 0),
                    kind=str(row.get("kind") or "reply"),  # type: ignore[arg-type]
                    tool=str(row.get("tool") or ""),
                    ok=bool(row.get("ok", True)),
                    decision=str(row.get("decision") or ""),
                )
                for row in plan
            ]
            trace_id = str(plan[0].get("trace_id") or "") if plan else ""
            for step in steps:
                step_options = activity_options(cfg, trace_id, step)
                try:
                    payload = await temporal_workflow.execute_activity(
                        "turn.step",
                        {"step": step.__dict__},
                        **step_options,
                    )
                    results[f"{step.seq}:{step.kind}"] = payload
                except Exception as exc:  # noqa: BLE001 — решение о компенсации принимает процесс, не exception
                    # патч-ветка: «предупредить вместо тихого отката» появилась позже и не имеет
                    # права менять поведение старых экземпляров — ровно для этого и patched()
                    if temporal_workflow.patched(PATCHES[0] if PATCHES else "reply-compensation"):
                        await temporal_workflow.execute_activity(
                            "turn.compensate",
                            {"error": str(exc)[:400], "done": list(results)},
                            id=f"{trace_id}:compensation",
                            start_to_close_timeout=30.0,
                        )
                        self._compensation_sent = True
                    raise
            return {
                "code_version": cfg.code_version,
                "steps": len(steps),
                "results": results,
                "compensated": self._compensation_sent,
            }

    @temporal_activity.defn(name="turn.step")
    async def turn_step(
        payload: dict[str, Any],
    ) -> dict[str, Any]:  # pragma: no cover - исполняет activity-воркер
        raise TemporalUnavailable(
            "activity зарегистрирована в другом процессе (worker);"
            " внутри воркфлоу её исполнять нечем"
        )

    @temporal_activity.defn(name="turn.compensate")
    async def turn_compensate(payload: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        raise TemporalUnavailable("см. turn.step")

    return AgentTurnWorkflow, turn_step, turn_compensate
