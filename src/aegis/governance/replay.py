"""Воспроизведение хода (M1): тот же вход, замороженный мир, сравнение ответов.

«Почему ты тогда так ответил» без replay — это ответ по хэшами либо по фантазии модели. Здесь
сравнивается по записи: из журнала достаётся точный список сообщений, из него убирается
последний ответ модели, и запрос повторяется с теми же параметрами, но **без инструментов**.
Записанные результаты инструментов уже стоят в контексте, поэтому разойтись может только решение
модели, а не то, что интернет сегодня отвечает иначе. В этом смысл: отличить «модель стала
другой» от «данные поменялись». Сверять сегодняшний ответ с вчерашним, когда поиск выдаёт другие
страницы, бессмысленно.

Судья — модель (fast), а не diff по строкам: формулировки имеют право различаться, числа, даты и
наличие действия — нет. Критерий эквивалентности живёт в промпте ``repro/judge``, а не в коде, и
потому промпт версионируется файлом: поменял критерий — получил другую версию, и в журнале это
видно.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import BaseModel, Field, ValidationError

from aegis.governance.recorder import DecisionRecorder
from aegis.platform.gateway.client import ModelGateway, ModelUnavailable
from aegis.platform.prompts import load as load_prompt

__all__ = ["ReplayJudgement", "ReplayReport", "replay_trace", "trim_to_frozen_world"]

log = structlog.get_logger(__name__)


class ReplayJudgement(BaseModel):
    """Вердикт судьи — схемой, а не «найди в тексте слово equivalent».

    ``critical`` отдельным полем потому, что «ответы разошлись» и «ответы разошлись в деньгах» — это
    разные инциденты: первое объясняется температурой, второе требует разбора.
    """

    equivalent: bool
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    differences: list[str] = Field(default_factory=list)
    critical: bool = False


@dataclass(slots=True)
class ReplayReport:
    trace_id: str
    ok: bool
    reason: str = ""
    model_then: str | None = None
    model_now: str | None = None
    judged: bool = False
    equivalent: bool | None = None
    score: float = 0.0
    critical: bool = False
    differences: list[str] = field(default_factory=list)
    original: str = ""
    replayed: str = ""
    dropped_messages: int = 0
    cost_usd: float = 0.0

    @property
    def model_changed(self) -> bool:
        return bool(self.model_then and self.model_now and self.model_then != self.model_now)

    def as_text(self) -> str:
        head = f"replay {self.trace_id[:8]}"
        if not self.ok:
            return f"{head}: не удалось — {self.reason}"
        lines = [head]
        if self.model_changed:
            lines.append(f"модель сменилась: была {self.model_then}, сейчас {self.model_now}")
        else:
            lines.append(f"та же модель: {self.model_now or self.model_then}")
        if not self.judged:
            lines.append(f"судья не отвечал ({self.reason or 'нет ответа'}); тексты ниже")
        else:
            verdict = "эквивалентно" if self.equivalent else "НЕ эквивалентно"
            if self.critical:
                verdict += " (расхождение существенное)"
            lines.append(f"{verdict}, score {self.score:.2f}")
            for diff in self.differences[:5]:
                lines.append(f"· {diff[:200]}")
        if self.dropped_messages:
            lines.append(f"из входа убрано хвостовых сообщений: {self.dropped_messages}")
        return "\n".join(lines)


async def replay_trace(
    trace_id: str,
    *,
    recorder: DecisionRecorder,
    gateway: ModelGateway,
    judge_role: str = "fast",
    replay_role: str | None = None,
) -> ReplayReport:
    """Повторить ход и сравнить. Никаких инструментов не исполняем — мир заморожен.

    ``replay_role`` по умолчанию берётся из записанного вызова: сравнивать «мозг» с «быстрым» — это
    сравнение двух разных моделей, а не воспроизводимость.
    """
    records = await recorder.records_for_trace(trace_id)
    if not records:
        return ReplayReport(
            trace_id=trace_id,
            ok=False,
            reason="записей нет: журнал включён позже этого хода либо repro_enabled выключен",
        )
    turn = next((row for row in reversed(records) if row.get("kind") == "turn_summary"), None)
    if turn is None:
        return ReplayReport(
            trace_id=trace_id, ok=False, reason="в журнале нет turn_summary этого хода"
        )
    messages = await recorder.record_input(turn)
    if not isinstance(messages, list) or not messages:
        return ReplayReport(
            trace_id=trace_id,
            ok=False,
            reason=(
                "вход хода не восстановить: блоб утрачен или запись была усечена лимитом "
                "repro_max_blob_bytes"
            ),
        )
    original = str(turn.get("output") or "")
    frozen, dropped = trim_to_frozen_world(messages)
    if not frozen:
        return ReplayReport(trace_id=trace_id, ok=False, reason="после обрезки нечего отправлять")

    params = _replay_params(records)
    role = replay_role or str(params.get("role") or "brain")
    temperature = float(params.get("temperature") or 0.3)
    thinking = bool(params.get("thinking", False))
    try:
        result = await gateway.chat(
            role,  # type: ignore[arg-type]
            frozen,
            tools=None,
            thinking=thinking,
            temperature=temperature,
            trace_id=trace_id,
        )
    except ModelUnavailable as exc:
        return ReplayReport(
            trace_id=trace_id,
            ok=False,
            reason=f"модель недоступна: {exc}"[:300],
            model_then=turn.get("model"),
        )
    replayed = result.content or ""
    report = ReplayReport(
        trace_id=trace_id,
        ok=True,
        model_then=turn.get("model"),
        model_now=result.model,
        original=original,
        replayed=replayed,
        dropped_messages=dropped,
        cost_usd=result.cost_usd,
    )
    await _judge(report, gateway=gateway, role=judge_role)
    return report


async def _judge(
    report: ReplayReport, *, gateway: ModelGateway, role: str
) -> ReplayJudgement | None:
    try:
        prompt = load_prompt("repro/judge")
    except FileNotFoundError as exc:
        report.reason = f"нет промпта судьи: {exc}"
        return None
    text = prompt.render(original=report.original or "(пусто)", replay=report.replayed or "(пусто)")
    try:
        judgement = await gateway.chat_json(
            role,  # type: ignore[arg-type]
            [{"role": "user", "content": text}],
            ReplayJudgement,
            thinking=prompt.thinking,
            temperature=prompt.temperature,
            trace_id=report.trace_id,
        )
    except (ValidationError, ModelUnavailable, ValueError) as exc:
        report.reason = f"судья не ответил валидно: {type(exc).__name__}: {str(exc)[:200]}"
        return None
    report.judged = True
    report.equivalent = judgement.equivalent
    report.score = judgement.score
    report.critical = judgement.critical
    report.differences = [item[:300] for item in judgement.differences[:8]]
    return judgement


def _replay_params(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Параметры из последнего записанного llm_call: температура, роль, thinking.

    Если снимков запроса нет (repro_record_payload выключен), воспроизводим дефолтами и честно
    возвращаем пустой thinking: «та же ли это настройка» в отчёте видно по отсутствию model_then.
    """
    out: dict[str, Any] = {}
    for row in reversed(records):
        if row.get("kind") != "llm_call":
            continue
        params = row.get("params") or {}
        out["role"] = params.get("role") or "brain"
        out["temperature"] = params.get("temperature", 0.3)
        out["thinking"] = bool(params.get("thinking"))
        request = row.get("request")
        if isinstance(request, dict):
            extra = request.get("extra_body") or {}
            thinking = extra.get("thinking") if isinstance(extra, dict) else None
            out["thinking"] = bool(isinstance(thinking, dict) and thinking.get("type") == "enabled")
            out["temperature"] = request.get("temperature", out["temperature"])
        return out
    return out


def trim_to_frozen_world(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Убрать из входа хвостовой ответ модели: воспроизводим «что бы она сказала на том входе».

    Срезаем с конца только assistant без tool_calls — это и есть «ответ». Всё остальное (история,
    пользователь, вызовы инструментов и их записанные результаты) остаётся: это и есть мир.

    """
    out = list(messages)
    dropped = 0
    # Ровно один хвост: _loop дописывает assistant с tool_calls, затем tool-результаты, и только
    # потом финальный assistant без tool_calls. Срезать больше — значит менять сам вход.
    if out and out[-1].get("role") == "assistant" and not out[-1].get("tool_calls"):
        out.pop()
        dropped = 1
    return out, dropped
