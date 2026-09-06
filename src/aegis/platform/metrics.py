"""Метрики приложения (F7): счётчики и гистограммы, живущие ровно столько, сколько нужно SLO.

Зачем свой слой, а не OTel-агент: тонкий аккумулятор принципу 5 не противоречит, а вот
зависимость ответа владельца от коллектора — противоречит. Здесь реестр — in-process аккумулятор;
samples
сбрасываются в ``platform.metric_samples`` тиком (или по ``aegis metrics flush``), SLO-окна
считаются с этой таблицы и из журнала. Формат выгрузки — Prometheus text и OTLP-shaped JSON:
второй вариант отдаётся уже существующему OTLP-экспортёру (путь Langfuse, ADR-0014) без нового
агента на проде.

Гистограмма — с фиксированными бакетами: «p95 из метрик» обязан воспроизводиться пересчётом
сэмплов, поэтому бакеты — часть контракта, а не деталь реализации.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import orjson
from sqlalchemy import text

from aegis.platform.db import SessionFactory, session

__all__ = [
    "Histogram",
    "MetricsRegistry",
    "SqlMetricsStore",
    "render_prometheus",
    "render_otlp_json",
]


def _labels_key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))


@dataclass(slots=True)
class Counter:
    name: str
    help: str = ""
    series: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)

    def inc(self, value: float = 1.0, **labels: str) -> None:
        self.series[_labels_key(labels)] = self.series.get(_labels_key(labels), 0.0) + value

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "name": self.name,
                "kind": "counter",
                "labels": dict(labels),
                "count": value,
                "sum": value,
                "buckets": [],
            }
            for labels, value in self.series.items()
        ]


#: дефолтные бакеты длительностей (мс): достаточно для p95 по SLO-целям 1–30 с, и это не
#: «бесконечный экспоненциальный ряд», который никто не читает
DEFAULT_LATENCY_BUCKETS: tuple[float, ...] = (
    250,
    500,
    1000,
    2000,
    4000,
    8000,
    15000,
    30000,
    60000,
)


@dataclass(slots=True)
class Histogram:
    name: str
    help: str = ""
    buckets: tuple[float, ...] = DEFAULT_LATENCY_BUCKETS
    series: dict[tuple[tuple[str, str], ...], list[int]] = field(default_factory=dict)
    sums: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)

    def observe(self, value: float, **labels: str) -> None:
        key = _labels_key(labels)
        counts = self.series.get(key)
        if counts is None:
            counts = [0] * (len(self.buckets) + 1)
            self.series[key] = counts
        placed = False
        for index, bound in enumerate(self.buckets):
            if value <= bound:
                counts[index] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1  # +Inf: «не влез никуда» — это наблюдение, а не потеря
        self.sums[key] = self.sums.get(key, 0.0) + value

    def quantile(self, q: float, **labels: str) -> float:
        """Линейная интерполяция внутри бакета: p95 «в пределах бакета» честнее, чем «последний
        observed», и ровно так же воспроизводимо из сэмплов."""
        key = _labels_key(labels)
        counts = self.series.get(key)
        if not counts:
            return float("nan")
        total = sum(counts)
        if total <= 0:
            return float("nan")
        target = q * total
        acc = 0
        edges = list(self.buckets) + [math.inf]
        for index, bound in enumerate(edges):
            if acc + counts[index] >= target:
                lower = edges[index - 1] if index else 0.0
                upper = bound
                width = counts[index] or 1
                frac = (target - acc) / width
                if math.isinf(upper):
                    return float(upper) if frac > 0 else float(lower)
                return float(lower + (upper - lower) * frac)
            acc += counts[index]
        return float(edges[-2])  # не достижимо, но mypy требует возврата

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "name": self.name,
                "kind": "histogram",
                "labels": dict(labels),
                "count": sum(counts),
                "sum": self.sums.get(labels, 0.0),
                "buckets": list(self.buckets),
                "counts": list(counts),
            }
            for labels, counts in self.series.items()
        ]


@dataclass(slots=True)
class MetricSample:
    name: str
    kind: str
    labels: dict[str, str]
    count: float
    sum_value: float
    buckets: list[float]
    counts: list[int]

    def as_row(self, period_start: str, period_end: str, source: str) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "labels": orjson.dumps(self.labels).decode(),
            "count": self.count,
            "sum_value": self.sum_value,
            "buckets": orjson.dumps(self.buckets).decode(),
            "counts": orjson.dumps(self.counts).decode(),
            "period_start": period_start,
            "period_end": period_end,
            "source": source,
        }


class MetricsRegistry:
    """Счётчики и гистограммы процесса. Поток-безопасен по построению: одна event loop."""

    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}
        self._flushed_at = 0.0

    def counter(self, name: str, help: str = "") -> Counter:  # noqa: A002 - терминоложия Prometheus
        item = self._counters.get(name)
        if item is None:
            item = Counter(name=name, help=help)
            self._counters[name] = item
        return item

    def histogram(
        self, name: str, help: str = "", buckets: tuple[float, ...] = DEFAULT_LATENCY_BUCKETS
    ) -> Histogram:
        item = self._histograms.get(name)
        if item is None:
            item = Histogram(name=name, help=help, buckets=buckets)
            self._histograms[name] = item
        return item

    def samples(self) -> list[MetricSample]:
        out: list[MetricSample] = []
        for entry in [c.snapshot() for c in self._counters.values()] + [
            h.snapshot() for h in self._histograms.values()
        ]:
            for row in entry:
                out.append(
                    MetricSample(
                        name=str(row["name"]),
                        kind=str(row["kind"]),
                        labels=dict(row["labels"]),
                        count=float(row["count"]),
                        sum_value=float(row.get("sum") or 0.0),
                        buckets=[float(b) for b in row.get("buckets") or []],
                        counts=[int(c) for c in row.get("counts") or []],
                    )
                )
        return out

    def snapshot(self) -> dict[str, Any]:
        return {"metrics": [vars(s) for s in self.samples()]}


def render_prometheus(samples: list[MetricSample]) -> str:
    """Prometheus text exposition: то, что умеет читать и node_exporter-туллинг, и curl."""
    lines: list[str] = []
    seen_help: set[str] = set()
    for sample in samples:
        if sample.name not in seen_help:
            seen_help.add(sample.name)
            lines.append(
                f"# TYPE {sample.name} {'counter' if sample.kind == 'counter' else 'histogram'}"
            )
        labels = ",".join(f'{k}="{v}"' for k, v in sorted(sample.labels.items()))
        suffix = f"{{{labels}}}" if labels else ""
        if sample.kind == "counter":
            lines.append(f"{sample.name}{suffix} {sample.count}")
            continue
        cumulative = 0
        for bound, value in zip(sample.buckets, sample.counts[:-1], strict=False):
            cumulative += value
            le = f"{bound:g}"
            bucket_labels = f'{labels},le="{le}"' if labels else f'le="{le}"'
            lines.append(f"{sample.name}_bucket{{{bucket_labels}}} {cumulative}")
        inf_labels = f'{labels},le="+Inf"' if labels else 'le="+Inf"'
        lines.append(f"{sample.name}_bucket{{{inf_labels}}} {int(sample.count)}")
        lines.append(f"{sample.name}_sum{suffix} {sample.sum_value}")
        lines.append(f"{sample.name}_count{suffix} {int(sample.count)}")
    return "\n".join(lines) + "\n"


def render_otlp_json(samples: list[MetricSample], *, service: str = "aegis") -> dict[str, Any]:
    """OTLP metrics-shaped документ: тот же транспорт, что у трейсов (ADR-0014), без агента.

    Совместимость с реальным OTLP-collector — задача приёмника: формат намеренно минимальный
    (sum + histogram data points), и в RUNBOOK помечено, что экспортёр включается явным
    ``AEGIS_OTLP_ENDPOINT``.
    """
    metrics: list[dict[str, Any]] = []
    for sample in samples:
        base = {"name": sample.name}
        if sample.kind == "counter":
            base["sum"] = {
                "dataPoints": [
                    {
                        "attributes": [
                            {"key": k, "value": {"stringValue": v}}
                            for k, v in sorted(sample.labels.items())
                        ],
                        "asInt": int(sample.count),
                    }
                ],
                "aggregationTemporality": 2,
                "isMonotonic": True,
            }
        else:
            base["histogram"] = {
                "dataPoints": [
                    {
                        "attributes": [
                            {"key": k, "value": {"stringValue": v}}
                            for k, v in sorted(sample.labels.items())
                        ],
                        "count": int(sample.count),
                        "sum": sample.sum_value,
                        "explicitBounds": sample.buckets,
                        "bucketCounts": sample.counts,
                    }
                ],
                "aggregationTemporality": 2,
            }
        metrics.append(base)
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": service}}]
                },
                "scopeMetrics": [{"scope": {"name": "aegis"}, "metrics": metrics}],
            }
        ]
    }


class SqlMetricsStore:
    """Сброс снапшота в ``platform.metric_samples`` (для SLO-окон) и чтение окон.

    Only-insert: историчность — смысл таблицы; правки нет, и append-only-триггер вешать не на что
    менять (строки «обновлять» нечем)."""

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._sm = session_factory

    def _session(self) -> Any:
        return self._sm() if self._sm is not None else session()

    async def flush(self, samples: list[MetricSample], *, source: str = "aegis") -> int:
        if not samples:
            return 0
        sql = text(
            """
            INSERT INTO platform.metric_samples
                (name, kind, labels, count, sum_value, buckets, counts, period_start,
                 period_end, source)
            VALUES (:name, :kind, CAST(:labels AS jsonb), :count, :sum_value,
                    CAST(:buckets AS jsonb), CAST(:counts AS jsonb),
                    now() - interval '15 minutes', now(), :source)
            """
        )
        async with self._session() as s:
            for sample in samples:
                await s.execute(sql, sample.as_row("", "", source))
            await s.commit()
        return len(samples)

    async def window(
        self,
        name: str,
        *,
        minutes: int = 60,
        label: str | None = None,
        label_value: str | None = None,
    ) -> dict[str, Any]:
        """Сумма счётчика / слияние бакетов гистограммы за окно. Именно по этому читают SLO."""
        sql = text(
            """
            SELECT kind, labels, count, sum_value, buckets, counts
              FROM platform.metric_samples
             WHERE name = :name AND period_end > now() - make_interval(mins => :minutes)
            """
        )
        async with self._session() as s:
            rows = (await s.execute(sql.bindparams(name=name, minutes=int(minutes)))).mappings()
            kind = "counter"
            total = 0.0
            sum_value = 0.0
            merged_counts: list[int] = []
            buckets: list[float] = []
            for row in rows:
                labels = dict(row["labels"] or {})
                if (
                    label is not None
                    and label_value is not None
                    and labels.get(label) != label_value
                ):
                    continue
                kind = str(row["kind"])
                total += float(row["count"] or 0)
                sum_value += float(row["sum_value"] or 0)
                if kind == "histogram":
                    row_counts = [int(c) for c in (row["counts"] or [])]
                    row_buckets = [float(b) for b in (row["buckets"] or [])]
                    if not merged_counts:
                        merged_counts = row_counts
                        buckets = row_buckets
                    elif len(row_counts) == len(merged_counts):
                        merged_counts = [
                            a + b for a, b in zip(merged_counts, row_counts, strict=True)
                        ]
            return {
                "name": name,
                "kind": kind,
                "count": total,
                "sum": sum_value,
                "buckets": buckets,
                "counts": merged_counts,
            }
