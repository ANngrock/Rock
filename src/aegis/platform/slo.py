"""SLO как код (F7): цели, окна ошибок и burn-rate — без «мне кажется, что стало хуже».

Три обещания, которые здесь держатся:

* **бюджет ошибок — арифметика, а не мнение.** ``objective=0.99`` на окне 28 дней означает:
  «1% ходов имеют право быть плохими, дальше — инцидент». ``consumed/total`` бюджета видно в
  ``aegis slo evaluate``, а не только на дашборде, который никто не открыл;
* **burn-rate вместо «среднее за сутки».** 2% плохих ответов за вечер и 2% за пять минут — это
  разные события. Быстро сжигаем бюджет (fast window: 1h/5m) → paging; медленно (6h/30m) —
  тикет в рабочее время. Пороги в ``deploy/slo.yml``, файл же — источник алертов:
  ``aegis slo alerts`` рендерит ``deploy/slo.alerts.yml`` (CI проверяет, что он не
  рассинхронизирован);
* **право на деградацию включено в контракт.** У каждого SLO поле ``degrade`` — какие ветки
  приложения переключаются при провале (стриминг → обычный ответ, веб-инструменты → «не нашёл»).
  Именно этот список, и ничего кроме него: тест «деградация включает ровно то, что объявлено»
  сверяет факт переключения с файлом. Факт попадает в журнал
  (:class:`~aegis.governance.degradation`),
  а не только в метрику.

Модуль чистый: синтетические окна (генератор в тестах) прогоняются без БД и сети — то же
вычисление, что дёргает CLI.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "SUPPORTED_SWITCHES",
    "SloError",
    "SloReport",
    "SloSet",
    "SloSpec",
    "WindowStats",
    "desired_switches",
    "evaluate_set",
    "evaluate_slo",
    "load_slo_file",
    "render_alerts",
    "summarize",
]

#: переключатели, которые деградация имеет право трогать. Новое имя сюда вписывается вместе
#: с обработчиком в supervisor: «объявлено, но не исполняемо» — источник ложного спокойствия
SUPPORTED_SWITCHES: frozenset[str] = frozenset(
    {"streaming", "web_tools", "verify", "fast_route_only"}
)

_BUDGET_PAGE = 14.4  # сжигание 2× недельного бюджета за 1 час — ночной page
_BUDGET_TICKET = 6.0  # 1× недельного за 6 часов — рабочее время


class SloError(ValueError):
    """Плохой slo.yml: молча игнорировать «неполный контракт наблюдаемости» нельзя."""


@dataclass(frozen=True, slots=True)
class WindowStats:
    """Наблюдение за окном: доля «хороших» событий (ratio) или перцентиль (quantile)."""

    total: float = 0.0
    good: float = 0.0
    quantile_value: float | None = None

    def bad_ratio(self) -> float:
        if self.total <= 0:
            return 0.0
        return max(0.0, 1.0 - self.good / self.total)


@dataclass(frozen=True, slots=True)
class BurnRate:
    severity: str  # 'page' | 'ticket'
    short_minutes: int
    long_minutes: int
    budget_fraction: float

    @property
    def threshold(self) -> float:
        """Во сколько раз «плохо» должно быть быстрее среднего, чтобы сжигать budget_fraction
        месячного окна за short-окно: 14.4 при 1h — это 2× недельного бюджета."""
        hours = max(self.short_minutes, 1) / 60.0
        weeks = 28.0 / (24.0 * 7.0)
        return self.budget_fraction / (hours / (24.0 * weeks)) if hours else self.budget_fraction


@dataclass(frozen=True, slots=True)
class SloSpec:
    name: str
    description: str
    objective: float  # доля хороших (0..1)
    indicator: Mapping[str, Any]  # {'type': 'ratio'|'quantile', ...}
    window_days: int = 28
    burn_rates: tuple[BurnRate, ...] = field(default_factory=tuple)
    degrade: tuple[str, ...] = ()

    @property
    def is_quantile(self) -> bool:
        return str(self.indicator.get("type", "ratio")) == "quantile"

    @property
    def metric(self) -> str:
        return str(self.indicator.get("metric", ""))

    @property
    def threshold(self) -> float:
        return float(self.indicator.get("threshold", 0.0) or 0.0)

    @property
    def quantile(self) -> float:
        return float(self.indicator.get("quantile", 0.95) or 0.95)

    def ok_window(self, stats: WindowStats) -> bool:
        if stats.total <= 0:
            return True  # нет данных — нет и провала: SLO не обязан врать «всё плохо» на пустоте
        if self.is_quantile:
            value = stats.quantile_value
            if value is None:
                return stats.bad_ratio() <= (1.0 - self.objective)
            return value <= self.threshold
        return stats.bad_ratio() <= (1.0 - self.objective)


@dataclass(frozen=True, slots=True)
class SloSet:
    version: int
    slos: tuple[SloSpec, ...]

    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.slos)


@dataclass(slots=True)
class SloReport:
    """Итог оценки одного SLO. ``degrade`` — что приложение переключит прямо сейчас."""

    name: str
    ok: bool
    objective: float
    window: str = ""
    total: float = 0.0
    bad: float = 0.0
    budget_total: float = 0.0
    budget_consumed: float = 0.0
    burn_rates: dict[str, float] = field(default_factory=dict)
    page: bool = False
    ticket: bool = False
    degrade: tuple[str, ...] = ()
    detail: str = ""

    @property
    def failing(self) -> bool:
        return not self.ok


def evaluate_slo(
    spec: SloSpec,
    window: WindowStats,
    *,
    short: WindowStats | None = None,
    long: WindowStats | None = None,
) -> SloReport:
    """Один SLO против наблюдений окна (+ короткие окна для burn-rate, если даны)."""
    ok = spec.ok_window(window)
    bad = max(window.total - window.good, 0.0)
    budget_total = window.total * (1.0 - spec.objective)
    report = SloReport(
        name=spec.name,
        ok=ok,
        objective=spec.objective,
        total=window.total,
        bad=bad,
        budget_total=budget_total,
        budget_consumed=bad,
        degrade=spec.degrade,
        detail=(
            f"плохо {bad:g}/{window.total:g}"
            + (
                f", p{spec.quantile * 100:g}={window.quantile_value:.0f}"
                if window.quantile_value is not None
                else ""
            )
        ),
    )
    if budget_total > 0:
        report.burn_rates["window"] = round(bad / budget_total, 4) if budget_total else 0.0
    if not ok and (short is not None or long is not None):
        # burn-rate: во сколько раз текущий темп плохих событий быстрее темпа бюджета
        allowed = max((1.0 - spec.objective), 1e-9)
        for label, stats in (("short", short), ("long", long)):
            if stats is None or stats.total <= 0:
                continue
            rate = stats.bad_ratio()
            report.burn_rates[f"{label}_rate"] = round(rate / allowed, 4)
    for burn in spec.burn_rates:
        factor = report.burn_rates.get("short_rate", report.burn_rates.get("window", 0.0))
        long_factor = report.burn_rates.get("long_rate", factor)
        if burn.severity == "page" and factor >= burn.budget_fraction:
            report.page = True
        if burn.severity == "ticket" and long_factor >= burn.budget_fraction:
            report.ticket = True
    return report


def evaluate_set(
    slo_set: SloSet,
    observations: Mapping[str, WindowStats],
    *,
    short: Mapping[str, WindowStats] | None = None,
    long: Mapping[str, WindowStats] | None = None,
) -> list[SloReport]:
    out: list[SloReport] = []
    for spec in slo_set.slos:
        stats = observations.get(spec.name)
        if stats is None:
            out.append(
                SloReport(
                    name=spec.name,
                    ok=True,
                    objective=spec.objective,
                    detail="нет наблюдений за окном (источник молчал — не считаем провалом)",
                    degrade=spec.degrade,
                )
            )
            continue
        out.append(
            evaluate_slo(
                spec,
                stats,
                short=(short or {}).get(spec.name),
                long=(long or {}).get(spec.name),
            )
        )
    return out


def desired_switches(reports: Iterable[SloReport]) -> dict[str, bool]:
    """Что деградация обязана включить/выключить. Ровно то, что объявлено в slo.yml.

    Возвращает маппинг переключателя → «требуется ли деградировать». Пустой — всё штатно:
    чужих ключей здесь взяться не может, и это проверяет тест (F7).
    """
    wanted: dict[str, bool] = {}
    for report in reports:
        for switch in report.degrade:
            if report.failing:
                wanted[switch] = True
            else:
                wanted.setdefault(switch, False)
    return wanted


# ------------------------------------------------------------------ загрузка и алерты


def load_slo_file(path: str | Path) -> SloSet:
    """``deploy/slo.yml`` → :class:`SloSet`. Валидация злая: опечатка в objective дороже падения
    CI."""
    file = Path(path)
    if not file.is_file():
        raise SloError(f"файл SLO не найден: {file}")
    import yaml  # noqa: PLC0415 — формат файла, а не runtime-зависимость пути ответа

    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    raw_slos = data.get("slos")
    if not isinstance(raw_slos, list) or not raw_slos:
        raise SloError(f"{file}: slos — непустой список")
    specs: list[SloSpec] = []
    seen: set[str] = set()
    for item in raw_slos:
        name = str(item.get("name", ""))
        if not name or name in seen:
            raise SloError(f"{file}: имя SLO пусто или дублируется: {name!r}")
        seen.add(name)
        objective = float(item.get("objective", 0.0))
        if not 0 < objective < 1:
            raise SloError(f"{file}: {name}: objective обязан быть в (0, 1)")
        degrade = tuple(str(x) for x in (item.get("degrade") or []))
        unknown = [x for x in degrade if x not in SUPPORTED_SWITCHES]
        if unknown:
            raise SloError(
                f"{file}: {name}: деградация {unknown} не поддерживается; "
                f"известно: {sorted(SUPPORTED_SWITCHES)}"
            )
        burns: list[BurnRate] = []
        for burn in item.get("burn_rates") or []:
            short, _, long = str(burn.get("windows", "1h,5m")).partition(",")
            burns.append(
                BurnRate(
                    severity=str(burn.get("severity", "ticket")),
                    short_minutes=_minutes(short),
                    long_minutes=_minutes(long),
                    budget_fraction=float(burn.get("budget_fraction", _BUDGET_TICKET)),
                )
            )
        specs.append(
            SloSpec(
                name=name,
                description=str(item.get("description", "")),
                objective=objective,
                indicator=dict(item.get("indicator") or {}),
                window_days=int(item.get("window_days", 28)),
                burn_rates=tuple(burns),
                degrade=degrade,
            )
        )
    return SloSet(version=int(data.get("version", 1)), slos=tuple(specs))


def _minutes(value: str) -> int:
    text_value = str(value).strip().lower()
    if text_value.endswith("h"):
        return int(float(text_value[:-1] or 0) * 60)
    if text_value.endswith("m"):
        return int(float(text_value[:-1] or 0))
    if text_value.endswith("d"):
        return int(float(text_value[:-1] or 0) * 1440)
    return int(float(text_value or 60))


def render_alerts(slo_set: SloSet) -> str:
    """Алерт-правила из SLO (burn-rate, two-window methodology). Файл — артефакт, не вторая правда.

    Правило «fast» пейджит, «slow» открывает тикет; пороги берутся из ``burn_rates`` — то есть
    «изменили SLO, забыли алерты» становится невозможным: CI сверяет сгенерированный файл с
    закоммиченным (``tests/test_slo_alerts.py``).
    """
    lines = [
        "# СГЕНЕРИРОВАНО из deploy/slo.yml — не править руками: `aegis slo alerts`",
        "version: 1",
        "groups:",
        "  - name: aegis-slos",
        "    rules:",
    ]
    for spec in slo_set.slos:
        for burn in spec.burn_rates or (BurnRate("ticket", 360, 30, _BUDGET_TICKET),):
            lines += [
                f"      - alert: {spec.name.replace('-', '_')}_{burn.severity}",
                f'        expr: aegis:slo_bad_ratio{{slo="{spec.name}"}} > '
                f"{(1 - spec.objective) * burn.budget_fraction:.6g}",
                f"        for: {max(burn.long_minutes, 5)}m",
                "        labels:",
                f"          severity: {burn.severity}",
                f"          slo: {spec.name}",
                "        annotations:",
                f'          summary: "{spec.description or spec.name}"',
                "          description: >-",
                f"            burn-rate {burn.budget_fraction:g}× бюджета за {burn.short_minutes}m",
                f"            (окно {burn.long_minutes}m), objective {spec.objective:.3f}",
            ]
    return "\n".join(lines) + "\n"


def summarize(reports: Sequence[SloReport]) -> str:
    """Человекочитаемый итог для ``/status`` и ``aegis slo evaluate``."""
    if not reports:
        return "SLO не заведены: проверь deploy/slo.yml и AEGIS_SLO_PATH"
    out: list[str] = []
    for report in reports:
        flag = "✅" if report.ok else ("🔥" if (report.page or report.ticket) else "⚠️")
        budget = (
            f" · бюджет {report.budget_consumed:g}/{report.budget_total:.2f}"
            if report.budget_total
            else ""
        )
        out.append(f"{flag} {report.name}: {report.detail}{budget}")
    return "\n".join(out)
