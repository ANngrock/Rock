"""Детерминатор шагов хода (F8): план из ``(trace_id, seq)`` — один для Temporal и для локального
раннера.

Идемпотентность сайд-эффектов — не «попросить модель не повторять», а свойство плана:
``activity_id = f"{trace_id}:{seq}"`` выводится из детерминированных входов, поэтому повторный
запуск воркфлоу (или крах посреди шага) даёт те же id, а реестр (:mod:`aegis.workflows.ledger`)
— тот же результат без повторного исполнения. «Платёж не уйдёт дважды» — буквально про это.

Версионирование кода (patching) — маркер в плане: у долгой цепочки напоминаний/реплеев деплой не
должен менять порядок уже идущих экземпляров. Temporal требует ``workflow.patched(...)`` для
изменения детерминизма; локальный раннер сверяет тот же ``code_version`` и честно сообщает о
смешанных версиях — иначе «у нас есть воркфлоу» было бы вывеской.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "CODE_VERSION",
    "PATCHES",
    "PlannedStep",
    "StepKind",
    "plan_from_journal",
    "plan_digest",
    "step_id",
]

#: версия кода воркфлоу. Правка порядка/набора шагов = обязательный bump: dual-run сверяет планы
#  по этой строке, и «тихо переписали процесс» превращается в красный diff, а не в странный баг
CODE_VERSION = "turn-v1"

#: точки патчинга (temporal workflow.patched). Новая запись = новая ветка поведения старых ходов
PATCHES: tuple[str, ...] = ("reply-compensation",)

StepKind = Literal["policy", "tool", "reply", "verify"]


@dataclass(frozen=True, slots=True)
class PlannedStep:
    """Шаг плана. ``seq`` — номер шага хода (turn_no журнала), он же — аргумент детерминизма."""

    seq: int
    kind: StepKind
    tool: str = ""
    ok: bool = True
    decision: str = ""


def step_id(trace_id: str, seq: int, kind: StepKind, tool: str = "") -> str:
    """Канон id activity: ``{trace}:{seq}:{kind}[:{tool}]``.

    Формат — контракт: его пишут в ledger, по нему ищут «что уже исполнено», и «совпадение после
    краха» обязано быть посимвольным. Любое изменение формата = новый CODE_VERSION.
    """
    tail = f":{tool}" if tool else ""
    return f"{trace_id}:{seq}:{kind}{tail}"


def plan_from_journal(rows: Sequence[Mapping[str, Any]]) -> list[PlannedStep]:
    """Собрать план из записей журнала хода (kind/turn_no/params) — для dual-run и реплея.

    Порядок — по turn_no, стабильная сортировка по id внутри шага: шаг мог оставить несколько
    строк (policy + tool_run), и «что было вторым» обязано читаться из данных, а не из порядка
    выборки.
    """
    steps: list[PlannedStep] = []
    for row in sorted(rows, key=lambda r: (int(r.get("turn_no") or 0), str(r.get("id") or ""))):
        kind = str(row.get("kind") or "")
        if kind not in ("policy", "tool_run", "turn_summary", "verdict"):
            continue
        params = dict(row.get("params") or {})
        mapped: StepKind = "reply"
        if kind == "policy":
            mapped = "policy"
        elif kind == "tool_run":
            mapped = "tool"
        elif kind == "verdict":
            mapped = "verify"
        steps.append(
            PlannedStep(
                seq=int(row.get("turn_no") or 0),
                kind=mapped,
                tool=str(params.get("tool") or ""),
                ok=bool(params.get("ok", True)),
                decision=str(dict(row.get("policy") or {}).get("decision") or ""),
            )
        )
    return steps


def plan_digest(steps: Sequence[PlannedStep]) -> dict[str, Any]:
    """Компактное «что процесс собирается делать» — для отчётов dual-run, не для хэшей."""
    return {
        "code_version": CODE_VERSION,
        "steps": [
            {"seq": s.seq, "kind": s.kind, "tool": s.tool, "ok": s.ok, "decision": s.decision}
            for s in steps
        ],
    }
