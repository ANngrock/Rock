"""Инструменты самонаблюдения (M1): «почему ты так ответил», цел ли журнал, заякорь день.

Зачем инструментами, а не только CLI: владелец живёт в Telegram, и «объясни» должно работать там же,
где задан вопрос. CLI остаётся для владельца-администратора (cron, systemd, `aegis repro verify`).

Три правила, которые здесь держим:
1. ничего не выполняем повторно — только читаем записанное (side effects чужих инструментов в
   объяснении не нужны, они уже случились);
2. если данных нет — так и говорим («ход до включения журнала»), а не сочиняем правдоподобный ответ;
3. ответ модели — не источник истины про трассу: всё, что здесь сказано, взято из строк БД.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, cast

from pydantic import BaseModel, Field

from aegis.agents.tools.registry import ToolContext, ToolResult, registry
from aegis.governance.policy import Risk
from aegis.governance.recorder import DecisionRecorder

__all__ = ["anchor_journal", "explain_decision", "verify_integrity"]

_MAX_EXPLAIN = 3600


class ExplainArgs(BaseModel):
    trace_id: str = Field(
        default="",
        max_length=64,
        description="TRACE из /status или из хвоста сообщения; пусто — про последний ход",
    )


@registry.register(
    "explain_decision",
    "Объяснить ход по журналу: какие промпты и модель, что решили инструменты и почему "
    "(allow/confirm/deny). Только чтение записанного; если записи нет — так и сказать.",
    ExplainArgs,
)
async def explain_decision(args: ExplainArgs, ctx: ToolContext) -> ToolResult:
    repro = _repro(ctx)
    if repro is None:
        return ToolResult(_DISABLED)
    ref = args.trace_id.strip()
    if ref:
        matches = await repro.matching_traces(ref)
        if not matches:
            return ToolResult(
                f"Ход {ref} в журнале не найден. Он мог быть до включения журнала (REPRO_ENABLED), "
                "а 8 символов из хвоста сообщения — не весь UUID: уточни идентификатор."
            )
        if len(matches) > 1:
            return ToolResult(
                "Начало идентификатора подходит к нескольким ходам: "
                + ", ".join(m[:13] for m in matches)
                + ". Нужен полный trace_id — не угадывай, какой из них имели в виду."
            )
        trace_id = matches[0]
    else:
        trace_id = await repro.latest_trace(owner_id=ctx.owner_id) or ""
        if not trace_id:
            return ToolResult(
                "Ходов в журнале нет: записи включили позже этого сообщения (REPRO_ENABLED) либо "
                "ответ был детерминированным, без обращения к модели."
            )
    records = await repro.records_for_trace(trace_id)
    if not records:
        return ToolResult(
            f"Ход {trace_id[:8]} в журнале не найден. Причины: он был до включения журнала, "
            "или trace_id неполный (нужен целиком UUID из /status)."
        )
    return ToolResult(_render(trace_id, records))


class AnchorArgs(BaseModel):
    """Якорь берётся «за сегодня»: отдельный аргумент с датой означал бы, что можно задним числом
    пересчитать день — а смысл якоря ровно в том, что его нельзя."""


class VerifyArgs(BaseModel):
    days: int = Field(default=7, ge=1, le=366, description="За сколько дней сверять цепочку")


@registry.register(
    "verify_integrity",
    "Сверить целостность журнала решений за последние N дней: пересчитать хэш-цепочку, найти "
    "пропуски, утраченные блобы и усечённые записи.",
    VerifyArgs,
)
async def verify_integrity(args: VerifyArgs, ctx: ToolContext) -> ToolResult:
    repro = _repro(ctx)
    if repro is None:
        return ToolResult(_DISABLED)
    since = (date.today() - timedelta(days=args.days)).isoformat()
    report = await repro.verify(since=since)
    return ToolResult(f"Журнал за {args.days} дн.: {report.summary()}")


@registry.register(
    "anchor_journal",
    "Заякорить записи дня: посчитать мерклов корень и сохранить его в governance.anchors "
    "(дальше его можно сверять с внешней публикацией).",
    AnchorArgs,
    writes=True,
    risk=Risk.LOW,
)
async def anchor_journal(args: BaseModel, ctx: ToolContext) -> ToolResult:
    repro = _repro(ctx)
    if repro is None:
        return ToolResult(_DISABLED)
    report = await repro.anchor()
    if not report.ok:
        return ToolResult(f"Якорь не поставлен: {report.note or 'нет записей за день'}")
    return ToolResult(
        f"Заякорировано {report.records} записей за {report.day}\n"
        f"root={report.merkle_root}\n"
        "Сохрани этот корень (можно опубликовать во внешний каталог с датой — тогда задним числом "
        "изменить журнал не получится незаметно)."
    )


_DISABLED = (
    "Журнал решений выключен (REPRO_ENABLED=false) или БД недоступна: объяснять по записям нечего. "
    "Не выдумывай причину — так и скажи владельцу."
)


def _repro(ctx: ToolContext) -> DecisionRecorder | None:
    """Журнал из сервисов, если он включён.

    ``enabled`` проверяем здесь, а не в каждом handler'е: выключенный журнал обязан отвечать
    владельцу «записи нет», а не пустым объяснением, которое модель прикрыт догадкой.
    """
    services = getattr(ctx, "services", None)
    repro: Any = getattr(services, "repro", None)
    if repro is None or not repro.enabled:
        return None
    return cast("DecisionRecorder", repro)


def _render(trace_id: str, records: list[dict[str, Any]]) -> str:
    """Разбирание хода из строк таблицы — без догадок «что модель, вероятно, думала»."""
    lines = [f"ход {trace_id}"]
    for row in records:
        kind = str(row.get("kind") or "")
        params = row.get("params") or {}
        policy = row.get("policy") or {}
        step = row.get("turn_no")
        if kind == "llm_call":
            tok = params.get("tokens") or {}
            lines.append(
                f"[{step}] модель {row.get('model')}: {tok.get('prompt', '?')}→"
                f"{tok.get('completion', '?')} ток., ${row.get('cost_usd')}, "
                f"{row.get('latency_ms')} мс"
                + ("" if row.get("ok", True) else f" · ошибка: {str(row.get('note'))[:120]}")
            )
        elif kind == "tool_run":
            verdict = str(policy.get("decision") or "?")
            lines.append(
                f"[{step}] инструмент {params.get('tool')} → {verdict}"
                + (f" ({str(policy.get('reason'))[:120]})" if policy.get("reason") else "")
                + (f", риск {params.get('risk')}" if params.get("risk") else "")
            )
        elif kind == "policy":
            lines.append(
                f"[{step}] policy {params.get('action')}: {policy.get('decision')} — "
                f"{str(policy.get('reason'))[:160]}"
            )
        elif kind == "turn_summary":
            ids = row.get("prompt_ids") or []
            # ключ называется «id», а не «name»: это то же поле, что лежит в decision_records,
            # и объяснение обязано читать те же байты, что записал рекордер, а не его пересказ
            rendered = ", ".join(f"{i.get('id')}@{i.get('version')}" for i in ids[:6] if i)
            lines.append(
                f"[{step}] итог: модель {row.get('model')}, "
                f"{len(str(row.get('input') or ''))} симв. входа, "
                f"итераций {params.get('iterations')}, маршрут {params.get('route') or '-'}"
                + (f"; промпты: {rendered}" if rendered else "")
            )
            if row.get("truncated"):
                lines.append("⚠ содержимое усечено лимитом: ход воспроизводится не целиком")
        elif kind == "verdict":
            ids = row.get("prompt_ids") or []
            criterion = ", ".join(f"{i.get('id')}@{i.get('version')}" for i in ids[:3] if i)
            lines.append(
                f"[{step}] сверка ответа: {policy.get('decision')} — "
                f"{str(policy.get('reason'))[:160]}"
                + (f" (критерий: {criterion})" if criterion else " (критерий не записан)")
            )
        else:
            # незнакомый вид — это не «мелочь»: молча пропущенная строка журнала выглядит как
            # «ход состоял из того, что показалось», и объяснение теряет смысл
            lines.append(f"[{step}] запись вида «{kind or '?'}» — этот код её не разбирает")
    if len(records) == 1:
        lines.append("(записей мало — вероятно, ход был частично до включения журнала)")
    return "\n".join(lines)[:_MAX_EXPLAIN]
