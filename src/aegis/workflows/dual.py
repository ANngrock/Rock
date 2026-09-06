"""Dual-run «таймер/цикл vs воркфлоу» (F8): теневое сравнение до переключения.

Тот же приём, что переводил напоминания с asyncio-таймера на таблицу+тик: новый путь сначала
ничего не исполняет, а сравнивает план с тем, что уже сделал старый, и печатает расхождения.
Переключение «на воркфлоу» — отдельное, осознанное действие владельца, а не побочный эффект
merger'а.

Чистые функции над записями журнала: БД трогает вызывающий код (CLI), сравнение — здесь, и оно
же тестируется без стека.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aegis.workflows.plan import CODE_VERSION, plan_from_journal

__all__ = ["TraceComparison", "compare_trace", "summarize"]


@dataclass(slots=True)
class TraceComparison:
    trace_id: str
    code_version: str = CODE_VERSION
    only_in_journal: list[str] = field(default_factory=list)
    only_in_plan: list[str] = field(default_factory=list)
    order_mismatch: list[str] = field(default_factory=list)
    matched: int = 0

    @property
    def clean(self) -> bool:
        return not (self.only_in_journal or self.only_in_plan or self.order_mismatch)

    def as_row(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "clean": self.clean,
            "only_in_journal": list(self.only_in_journal),
            "only_in_plan": list(self.only_in_plan),
            "order_mismatch": list(self.order_mismatch),
            "matched": self.matched,
        }


def compare_trace(trace_id: str, journal_rows: Sequence[Mapping[str, Any]]) -> TraceComparison:
    """Сверить «что процесс сделал» (журнал) с «что процесс планирует» (тот же вход → план).

    Расхождение порядка — мягкий сигнал, а не поломка: два шага в одном номере хода (policy +
    tool_run) меняются местами без семантических последствий; шага нет вовсе — это уже
    «детерминатор разучился видеть то, что делает код», и именно это ищит dual-run.
    """
    plan_steps = plan_from_journal(journal_rows)
    journal_keys = [f"{r.get('turn_no')}:{r.get('kind')}" for r in journal_rows]
    plan_keys = [f"{s.seq}:{s.kind}" for s in plan_steps]
    comparison = TraceComparison(trace_id=trace_id)
    for key in journal_keys:
        if key not in plan_keys:
            comparison.only_in_journal.append(key)
    for key in plan_keys:
        if key not in journal_keys:
            comparison.only_in_plan.append(key)
    matched_journal = [k for k in journal_keys if k in plan_keys]
    matched_plan = [k for k in plan_keys if k in journal_keys]
    if matched_journal != matched_plan:
        comparison.order_mismatch.append("sequence differs")
    comparison.matched = len(set(matched_journal))
    return comparison


def summarize(comparisons: Sequence[TraceComparison]) -> dict[str, Any]:
    total = len(comparisons)
    clean = sum(1 for c in comparisons if c.clean)
    return {
        "traces": total,
        "clean": clean,
        "diverged": total - clean,
        "code_version": CODE_VERSION,
    }
