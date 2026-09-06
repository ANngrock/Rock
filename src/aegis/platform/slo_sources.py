"""Источники наблюдений для SLO (F7): окна из журнала, метрик и очередей.

Слои разделены специально: ``platform.slo`` — чистая арифметика бюджетов (её тестируют
синтетическими окнами), а здесь — SQL'и, из которых окна берутся. Новый SLO обязан получить
источник: если имени нет в ``JOURNAL_SOURCES``, он читается из ``platform.metric_samples`` по
``indicator.metric``; не нашёл ни там ни там — «источник молчит», и SLO НЕ считает это провалом
(см. :meth:`aegis.platform.slo.SloSpec.ok_window`). «Метрика пропала — приложение деградировало»
было бы самым изобретательным способом устроить недоступность своими руками.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from aegis.platform.db import SessionFactory, session
from aegis.platform.metrics import SqlMetricsStore
from aegis.platform.slo import SloSet, SloSpec, WindowStats

__all__ = ["Observation", "collect_observations", "summarize_observations"]

#: сигнатура источника: (session-подобный callable, минуты окна) → WindowStats
JournalSource = Callable[[Any, int], Awaitable[WindowStats]]


@dataclass(frozen=True, slots=True)
class Observation:
    spec: SloSpec
    window: WindowStats
    short: WindowStats | None = None
    long: WindowStats | None = None


async def _turn_latency(stats_smo: SessionFactory, minutes: int) -> WindowStats:
    sql = text(
        """
        SELECT
          count(*)::float AS total,
          count(*) FILTER (WHERE latency_ms <= 8000)::float AS good,
          percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95
        FROM governance.decision_records
        WHERE kind = 'turn_summary' AND created_at > now() - make_interval(mins => :m)
        """
    )
    async with stats_smo() as s:
        row = (await s.execute(sql.bindparams(m=int(minutes)))).mappings().first()
    if row is None or not row["total"]:
        return WindowStats()
    p95 = float(row["p95"] or 0.0)
    return WindowStats(total=float(row["total"]), good=float(row["good"] or 0), quantile_value=p95)


async def _unverified_share(stats_smo: SessionFactory, minutes: int) -> WindowStats:
    sql = text(
        """
        WITH turns AS (
            SELECT trace_id, max(seq) AS seq
              FROM governance.decision_records
             WHERE kind = 'turn_summary'
               AND created_at > now() - make_interval(mins => :m)
             GROUP BY trace_id
        ), judged AS (
            SELECT DISTINCT v.trace_id
              FROM governance.decision_records v
              JOIN turns t ON t.trace_id = v.trace_id
             WHERE v.kind = 'verdict'
        )
        SELECT count(*)::float AS total, count(j.trace_id)::float AS good
          FROM turns t LEFT JOIN judged j ON j.trace_id = t.trace_id
        """
    )
    async with stats_smo() as s:
        row = (await s.execute(sql.bindparams(m=int(minutes)))).mappings().first()
    if row is None or not row["total"]:
        return WindowStats()
    return WindowStats(total=float(row["total"]), good=float(row["good"] or 0))


async def _tool_failures(stats_smo: SessionFactory, minutes: int) -> WindowStats:
    sql = text(
        """
        SELECT count(*)::float AS total, count(*) FILTER (WHERE ok)::float AS good
          FROM governance.tool_runs
         WHERE created_at > now() - make_interval(mins => :m)
        """
    )
    async with stats_smo() as s:
        row = (await s.execute(sql.bindparams(m=int(minutes)))).mappings().first()
    if row is None or not row["total"]:
        return WindowStats()
    return WindowStats(total=float(row["total"]), good=float(row["good"] or 0))


async def _outbox_lag(stats_smo: SessionFactory, minutes: int) -> WindowStats:
    sql = text(
        """
        SELECT
          count(*)::float AS total,
          count(*) FILTER (WHERE EXTRACT(EPOCH FROM (o.published_at - e.occurred_at)) <= 60)::float
          AS good,
          percentile_cont(0.95) WITHIN GROUP (
              ORDER BY EXTRACT(EPOCH FROM (o.published_at - e.occurred_at))
          ) AS p95
        FROM platform.outbox o
        JOIN platform.events e ON e.id = o.event_id
        WHERE o.published_at IS NOT NULL
          AND e.occurred_at > now() - make_interval(mins => :m)
        """
    )
    async with stats_smo() as s:
        row = (await s.execute(sql.bindparams(m=int(minutes)))).mappings().first()
    if row is None or not row["total"]:
        return WindowStats()
    return WindowStats(
        total=float(row["total"]),
        good=float(row["good"] or 0),
        quantile_value=float(row["p95"] or 0),
    )


#: имя SLO → источник. Совпадение имён с ``deploy/slo.yml`` — контракт; гейт «новый SLO без
#: источника» живёт в тесте на соответствие файлов
JOURNAL_SOURCES: dict[str, JournalSource] = {
    "turn-latency-p95": _turn_latency,
    "unverified-answer-share": _unverified_share,
    "tool-failure-rate": _tool_failures,
    "outbox-lag-p95": _outbox_lag,
}


async def _from_metrics(store: SqlMetricsStore, spec: SloSpec, minutes: int) -> WindowStats:
    """Counter-based SLO: total = «все исходы» (хороший счётчик + bad_counter), good — хороший."""
    metric = spec.metric
    good_doc = await store.window(metric, minutes=minutes)
    bad_name = str(spec.indicator.get("bad_counter") or "")
    total = float(good_doc["count"])
    if bad_name:
        bad_doc = await store.window(bad_name, minutes=minutes)
        total += float(bad_doc["count"])
    if not total:
        return WindowStats()
    if spec.is_quantile:
        counts = good_doc.get("counts") or []
        buckets = good_doc.get("buckets") or []
        return WindowStats(
            total=total,
            good=total,
            quantile_value=_quantile_from_buckets(counts, buckets, spec.quantile),
        )
    return WindowStats(total=total, good=float(good_doc["count"]))


def _quantile_from_buckets(
    counts: Sequence[int] | list[int], buckets: Sequence[float], q: float
) -> float:
    if not counts:
        return float("nan")
    total = sum(counts)
    if total <= 0:
        return float("nan")
    target = q * total
    acc = 0
    edges = list(buckets) + [float("inf")]
    for index, bound in enumerate(edges):
        if acc + (counts[index] if index < len(counts) else 0) >= target:
            return float(bound)
        acc += counts[index] if index < len(counts) else 0
    return float(edges[-2]) if len(edges) >= 2 else float("nan")


async def collect_observations(
    slo_set: SloSet,
    *,
    window_minutes: int = 60,
    session_factory: SessionFactory | None = None,
    metrics: SqlMetricsStore | None = None,
) -> dict[str, Observation]:
    """Окно + короткие окна для burn-rate. Любой отказ источника — «молчит», не исключение.

    «молчит» = WindowStats(): SLO ок, деградация не включается. Логика одна для всех источников:
    сломанная наблюдаемость не имеет права включать автоматику, иначе инцидент метрик
    превратится в инцидент доступности (принцип 5 наоборот не работает: деградация обязана
    переживать отсутствие улучшений, а не устраивать их отсутствие).
    """
    smo = session_factory or session
    metrics_store = metrics or SqlMetricsStore(session_factory)
    out: dict[str, Observation] = {}
    for spec in slo_set.slos:
        source = JOURNAL_SOURCES.get(spec.name)

        async def _safe(minutes: int, *, src: Any = source, sp: Any = spec) -> WindowStats:
            try:
                if src is not None:
                    stats: WindowStats = await src(smo, minutes)
                    return stats
                return await _from_metrics(metrics_store, sp, minutes)
            except Exception:  # noqa: BLE001 — см. докстринг: источник обязан молчать, а не кричать
                return WindowStats()

        out[spec.name] = Observation(
            spec=spec,
            window=await _safe(window_minutes),
            short=await _safe(_burn_minutes(spec, "short")),
            long=await _safe(_burn_minutes(spec, "long")),
        )
    return out


def _burn_minutes(spec: SloSpec, which: str) -> int:
    if not spec.burn_rates:
        return 60
    burn = spec.burn_rates[0]
    return burn.short_minutes if which == "short" else burn.long_minutes


def summarize_observations(observations: Mapping[str, Observation]) -> dict[str, Any]:
    """Для ``doctor``/``aegis slo evaluate --json``: что реально удалось прочитать."""
    return {
        name: {
            "total": obs.window.total,
            "good": obs.window.good,
            "quantile": obs.window.quantile_value,
        }
        for name, obs in observations.items()
    }
