"""Тик парсера: «созревшие источники → свежие предметы → владелец уведомлён».

Тот же узор, что у remind/watches: один цикл в процессе бота, CLI-прогон (`aegis feed run`)
не создаёт дублей — созревшие ряды берутся SKIP LOCKED. Уведомление — через переданный
async send(text): run_feeds не знает про aiogram, в тестах — сборщик в список.

Новизна решается ДЕЙСТВИЕМ вставки (ON CONFLICT DO NOTHING RETURNING id), а не сравнением
«что было в прошлый раз»: курсор — оптимизация (не качать пагинацию заново), не источник
истины; дедупликация — ограничение схемы. Так «тик упал посередине» не порождает ни дублей,
ни молчаливых потерь.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
import structlog

from aegis.parsing.engine import ItemDraft, scan_source
from aegis.parsing.store import SourceRow

__all__ = ["FeedsReport", "ScanOutcome", "parse_now", "run_feeds"]

log = structlog.get_logger(__name__)

Send = Callable[[str], Awaitable[None]]
_PUSH_PER_SOURCE = 4  # «весь архив» в личку — отказ: четыре строки на источник, остальное в /menu


@dataclass(slots=True)
class ScanOutcome:
    source: str
    new: int = 0
    error: str = ""
    notes: list[str] = field(default_factory=list)

    def line(self) -> str:
        if self.error:
            return f"⚠️ {self.source}: {self.error[:120]}"
        return f"{self.source}: новых {self.new}" + (f" · {self.notes[0]}" if self.notes else "")


@dataclass(slots=True)
class FeedsReport:
    checked: int = 0
    errors: int = 0
    new_items: int = 0
    pushed: int = 0
    notes: list[str] = field(default_factory=list)
    outcomes: list[ScanOutcome] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {
            "checked": self.checked,
            "errors": self.errors,
            "new_items": self.new_items,
            "pushed": self.pushed,
        }


async def parse_now(cfg: Any, target: str, *, limit: int = 10) -> list[ItemDraft]:
    """Одноразовое «прочитай это» без БД: инструменты агента и CLI. Тот же движок, один вызов."""
    probe = SourceRow(
        id="00000000-0000-0000-0000-000000000000",
        owner_id=0,
        kind="auto",
        target=target,
        label="",
        interval_sec=900,
        last_check=None,
        last_ok=None,
        last_error="",
        cursor={},
    )
    drafts, _cursor, _notes = await scan_source(probe, cfg=cfg)
    return drafts[: max(1, limit)]


async def run_feeds(
    store: Any,
    *,
    owner_id: int,
    cfg: Any,
    cipher: Any = None,
    send: Send | None = None,
    limit: int = 8,
) -> FeedsReport:
    """Один проход: claim due → скан общим клиентом → вставка → (опционально) пуш владельцу."""
    report = FeedsReport()
    sources = await store.due_sources(limit=limit)
    if not sources:
        return report
    cap = int(getattr(cfg, "parser_max_items_per_source", 60))
    sem = asyncio.Semaphore(max(1, int(getattr(cfg, "parser_concurrency", 6))))
    timeout = float(getattr(cfg, "parser_timeout_s", 30.0))

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, max_redirects=0, verify=True
    ) as client:

        async def one(src: SourceRow) -> ScanOutcome:
            out = ScanOutcome(source=src.label or src.target)
            async with sem:
                try:
                    drafts, cursor, notes = await scan_source(src, cfg=cfg, client=client)
                    out.notes = [n for n in notes if n]
                    fresh = drafts[:cap]
                    if fresh:
                        items = [asdict(d) for d in fresh]
                        out.new = await store.add_items(source=src, items=items, cipher=cipher)
                    await store.touch_run(src.id, cursor=cursor or {})
                except Exception as exc:  # noqa: BLE001 — сбой источника не отменяет тик
                    out.error = f"{type(exc).__name__}: {str(exc)[:200]}"
                    with contextlib.suppress(Exception):
                        await store.touch_run(src.id, error=out.error)
            return out

        results = list(await asyncio.gather(*(one(s) for s in sources)))

    report.checked = len(sources)
    report.errors = sum(1 for r in results if r.error)
    report.new_items = sum(r.new for r in results)
    report.outcomes = results
    report.notes = [n for r in results for n in r.notes][:8]

    if send is not None and report.new_items:
        for src, res in zip(sources, results, strict=True):
            if res.new <= 0 or res.error:
                continue
            fresh = await store.list_items(
                owner_id=src.owner_id or int(owner_id),
                source_ref=src.id[:8],
                status="new",
                limit=_PUSH_PER_SOURCE,
                cipher=cipher,
            )
            if not fresh:
                continue
            head = f"📡 {src.label or src.target} — нового: {res.new}"
            lines = [head]
            for it in fresh:
                when = it.published_at.strftime("%d.%m %H:%M") if it.published_at else "сейчас"
                title = it.title[:180] or "(без заголовка)"
                lines.append(f"• [{when}] {title}")
                if it.url:
                    lines.append(f"  {it.url[:120]}")
            await send("\n".join(lines)[:3800])
            await store.mark_pushed(ids=[it.id for it in fresh])
            report.pushed += len(fresh)

    return report
