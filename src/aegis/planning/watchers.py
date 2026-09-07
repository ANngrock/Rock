"""Наблюдатели: «следи, пока не случится» — проверка условия по расписанию, доставка чужими руками.

Три решения, которые держат весь слой:

1. Наблюдатель НИЧЕГО не отправляет. Сработал — вставил напоминание с due_at=now в том же
   коммите; доставляет егоReminderDispatcher (2.9) со всеми fallback'ами. Условие «заодно и
   позвоним» здесь — это второй механизм доставки, а второй механизм однажды рассинхронизируется
   с первым (и сделает это ночью).
2. Текст страницы — чужие данные. Он участвует только в детерминированном сравнении и в отрывке
   для владельца; в промпт модели и в любую команду он не попадает никогда — иначе «найди слово
   Х на странице» превращается в «страница приказывает боту удалить заметки».
3. «changed» — хэш, а не «на глаз»: первое прохождение только засеивает baseline и НЕ стреляет,
   иначе любое наблюдение срабатывает в момент создания.

Пауза после MAX_FAILURES подряд — не перестраховка: мишень проверки могла переехать, а тик,
который каждый цикл ловит исключение и молча растит failures, обязан когда-то замолчать ГРОМКО
(в заметке тика и в doctor-строке), а не шуршать в логе вечно.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog
from sqlalchemy import text

from aegis.planning.reminders import (
    REMINDER_CHANNELS,
    build_insert_params,
    insert_reminder,
)
from aegis.platform.db import session

__all__ = [
    "MAX_FAILURES",
    "SqlWatchStore",
    "Watch",
    "WatchReport",
    "decide_after_check",
    "evaluate",
    "excerpt",
    "normalize_needle",
    "run_watches",
]

log = structlog.get_logger(__name__)

WATCH_KINDS = ("page", "search")
WATCH_MODES = ("contains", "regex", "changed")
MIN_INTERVAL_MINUTES = 5
#: десять подряд упавших проверок — это «мишень не там», пауза громче вечного шуршания
MAX_FAILURES = 10
#: аренда на время проверки: проверка = сеть (до 30 с), зависший процесс не должен блокировать
#: наблюдение дольше, чем на 5 минут;literal в _CLAIM_SQL (make_interval) — asyncpg и параметр
#: типа interval в text() acquainted плохо, а нам нужна именно константа
LEASE_MINUTES = 5


@dataclass(slots=True, frozen=True)
class Watch:
    """Строка planning.watches в памяти. Поля — ровно то, что нужно проверить и переармовать."""

    id: str
    owner_id: int
    title: str
    kind: str
    target: str
    mode: str
    needle: str | None
    interval_minutes: int
    channel: str
    repeat: bool
    status: str
    fire_at: datetime
    expires_at: datetime | None
    baseline_hash: str | None
    failures: int = 0

    @property
    def short_id(self) -> str:
        return self.id[:8]


def normalize_needle(mode: str, needle: str | None) -> str | None:
    """Условие проверяется при постановке. Regex, который падает в рантайме тика, — это баг
    пользователя, обнаруженный способом, при котором он ничего не увидит (исключение глотается
    тикером и прячется в last_error)."""
    if mode == "changed":
        return None
    raw = (needle or "").strip()
    if not raw:
        raise ValueError(f"режим «{mode}» требует условие: что искать на странице/в выдаче")
    if len(raw) > 400:
        raise ValueError("условие длиннее 400 символов — это уже не условие")
    if mode == "regex":
        try:
            re.compile(raw)
        except re.error as exc:
            raise ValueError(f"некорректное регулярное выражение: {exc}") from exc
    return raw


def _text_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:32]


def evaluate(
    mode: str, needle: str | None, body: str, baseline: str | None
) -> tuple[bool, str, str | None]:
    """Вердикт (hit, новый_хэш, доказательство). Доказательство — фрагмент, который увидит
    владелец; модель его не читает, команды из него не выполняются — это цитата для человека."""
    digest = _text_hash(body)
    if mode == "changed":
        if baseline is None:
            return False, digest, None  # засеваем эталон, не стреляем
        hit = digest != baseline
        # доказательство живёт ТОЛЬКО при попадании: proof на миссе — это мусор в отчётах
        return (
            hit,
            digest,
            (f"содержимое изменилось ({baseline[:8]}→{digest[:8]})" if hit else None),
        )
    if mode == "contains":
        assert needle is not None
        pos = body.lower().find(needle.lower())
        if pos < 0:
            return False, digest, None
        return True, digest, f"найдено «{needle}»"
    assert needle is not None
    match = re.search(needle, body, re.I)
    if match is None:
        return False, digest, None
    return True, digest, f"совпадение с образцом ({match.group(0)[:60]})"


def excerpt(body: str, around: str | None = None, width: int = 110) -> str:
    """Центр — вхождение (если задано), иначе — хвост: у «changed» хвост изменился чаще всего."""
    text = " ".join(body.split())
    if not text:
        return ""
    if around:
        pos = text.lower().find(around.lower())
        if pos >= 0:
            start = max(0, pos - width // 2)
            return ("…" if start else "") + text[start : start + width].strip() + "…"
    tail = text[-width:]
    return ("…" if len(text) > width else "") + tail


def fire_text(watch: Watch, proof: str, snippet: str) -> str:
    head = f"👁 {watch.title}: {proof}"
    return f"{head}{f' — «{snippet}»' if snippet else ''}"


def decide_after_check(
    hit: bool, *, now: datetime, interval_minutes: int, expires_at: datetime | None, repeat: bool
) -> str:
    """Чистое решение «что делать с наблюдением после вердикта» — здесь, а не в SQL: его обязан
    видеть тест без БД. one-shot попал — fired; expires отрезал горизонт — expired."""
    if hit and not repeat:
        return "fired"
    nxt = now + timedelta(minutes=interval_minutes)
    if expires_at is not None and nxt >= expires_at:
        return "expired"
    return "rearm"


class WatchChecker(Protocol):
    """Достаёт ТЕКСТ мишени. Сравнение — не здесь: у провайдера проверки одно умение."""

    async def body(self, watch: Watch) -> str: ...


class CheckError(RuntimeError):
    """Мишень недоступна/не настроена. Не провал владельца — штатная пауза с причиной."""


def checker_for(
    watch: Watch, *, page: WatchChecker | None = None, search: WatchChecker | None = None
) -> WatchChecker:
    """Сетевые проверки живут СНАРУЖИ домена (контракт слоёв: planning не трогает web);
    композиция — в ``aegis.agents.watch_checks.default_checkers()``."""
    checker = page if watch.kind == "page" else search
    if checker is None:
        raise CheckError(f"нет checker для kind={watch.kind}: проверка не будет выполнена вслепую")
    return checker


@dataclass(slots=True)
class WatchReport:
    checked: int = 0
    fired: int = 0
    rearmed: int = 0
    failed: int = 0
    paused: int = 0
    expired: int = 0
    notes: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "checked": self.checked,
            "fired": self.fired,
            "rearmed": self.rearmed,
            "failed": self.failed,
            "paused": self.paused,
            "expired": self.expired,
        }

    def summary(self) -> str:
        c = self.counts()
        return (
            f"проверено {c['checked']}, сработало {c['fired']}, продолжено {c['rearmed']}, "
            f"ошибок {c['failed']}, на паузе {c['paused']}, истекло {c['expired']}"
        )


_SELECT_COLS = """
            SELECT id::text AS id, owner_id, title, kind, target, mode, needle,
                   interval_minutes, channel, repeat, status, fire_at, expires_at,
                   baseline_hash, last_error, failures
              FROM planning.watches
"""

_CLAIM_SQL = """
            WITH picked AS (
                SELECT id FROM planning.watches
                 WHERE status = 'active' AND fire_at <= now()
                 ORDER BY fire_at
                 LIMIT :limit
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE planning.watches w
               SET fire_at = now() + make_interval(mins => 5), updated_at = now()
              FROM picked WHERE w.id = picked.id
            RETURNING w.id::text AS id, w.owner_id, w.title, w.kind, w.target, w.mode,
                      w.needle, w.interval_minutes, w.channel, w.repeat, w.status,
                      w.fire_at, w.expires_at, w.baseline_hash, w.failures
        """

_EXPIRE_SQL = """
            UPDATE planning.watches
               SET status = 'expired', updated_at = now()
             WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= now()
        """


def _watch(row: Any) -> Watch:
    return Watch(
        id=str(row["id"]),
        owner_id=int(row["owner_id"]),
        title=str(row["title"]),
        kind=str(row["kind"]),
        target=str(row["target"]),
        mode=str(row["mode"]),
        needle=row["needle"],
        interval_minutes=int(row["interval_minutes"]),
        channel=str(row["channel"]),
        repeat=bool(row["repeat"]),
        status=str(row["status"]),
        fire_at=row["fire_at"],
        expires_at=row["expires_at"],
        baseline_hash=row["baseline_hash"],
        failures=int(row["failures"] or 0),
    )


class SqlWatchStore:
    """Один вызов — одна транзакция (тот же контракт, что у напоминаний: тик переживает рестарт)."""

    def __init__(self, *, session_factory: Any = None) -> None:
        self._sm = session_factory

    def _session(self) -> Any:
        return self._sm() if self._sm is not None else session()

    async def add(
        self,
        *,
        owner_id: int,
        title: str,
        kind: str,
        target: str,
        mode: str = "contains",
        needle: str | None = None,
        interval_minutes: int = 15,
        channel: str = "message",
        repeat: bool = False,
        expires_at: datetime | None = None,
        trace_id: str | None = None,
    ) -> str:
        if kind not in WATCH_KINDS:
            raise ValueError(f"наблюдать умею за {WATCH_KINDS}")
        if mode not in WATCH_MODES:
            raise ValueError(f"режим обязан быть одним из {WATCH_MODES}")
        if channel not in REMINDER_CHANNELS:
            raise ValueError(f"канал доставки обязан быть одним из {REMINDER_CHANNELS}")
        if not 5 <= int(interval_minutes) <= 14400:
            raise ValueError("интервал — от 5 минут до 10 дней")
        clean_target = " ".join(str(target).split())
        if kind == "page" and not clean_target.lower().startswith(("http://", "https://")):
            raise ValueError("для страницы нужен полный http(s)-адрес")
        if not clean_target or len(clean_target) > 2000:
            raise ValueError("пустой или неприлично длинный адрес/запрос")
        clean_needle = normalize_needle(mode, needle)
        if expires_at is not None:
            if expires_at.tzinfo is None:
                raise ValueError("expires_at обязан быть tz-aware")
            if expires_at <= datetime.now(UTC) + timedelta(minutes=interval_minutes):
                raise ValueError(
                    "наблюдение умрёт до первой проверки: «до» должно быть дальше интервала"
                )
        title_clean = " ".join(str(title).split())[:200]
        if len(title_clean) < 3:
            raise ValueError("название наблюдения обязано быть читаемым (от 3 символов)")
        async with self._session() as s:
            row = (
                await s.execute(
                    text(
                        "INSERT INTO planning.watches (owner_id, title, kind, target, mode,"
                        " needle, interval_minutes, channel, repeat, expires_at, fire_at,"
                        " trace_id) VALUES (:owner_id, :title, :kind, :target, :mode, :needle,"
                        " :interval_minutes, :channel, :repeat, :expires_at, now() +"
                        " interval '1 minute', :trace_id) RETURNING id::text AS id"
                    ),
                    {
                        "owner_id": int(owner_id),
                        "title": title_clean,
                        "kind": kind,
                        "target": clean_target,
                        "mode": mode,
                        "needle": clean_needle,
                        "interval_minutes": int(interval_minutes),
                        "channel": channel,
                        "repeat": bool(repeat),
                        "expires_at": expires_at,
                        "trace_id": trace_id,
                    },
                )
            ).mappings()
            watch_id = str(row.one()["id"])
            await s.commit()
        return watch_id

    async def list_active(self, *, owner_id: int, limit: int = 20) -> list[dict[str, Any]]:
        async with self._session() as s:
            result = await s.execute(
                text(
                    _SELECT_COLS + " WHERE owner_id = :owner_id AND status IN ('active', 'paused')"
                    " ORDER BY fire_at LIMIT :limit"
                ),
                {"owner_id": int(owner_id), "limit": limit},
            )
            return [dict(r) for r in result.mappings().all()]

    async def set_status(self, *, owner_id: int, ref: str, status: str) -> str | None:
        """pause|resume|cancel по id или слову из названия (UX как у напоминаний)."""
        ref = ref.strip()
        by_id = bool(re.fullmatch(r"[0-9a-fA-F-]{8,36}", ref))
        where = "id::text LIKE :pat || '%'" if by_id else "btrim(title) ILIKE '%' || :pat || '%'"
        if status == "resume":  # «на паузе» — единственное, откуда возвращаются в активные
            payload_sql = (
                "UPDATE planning.watches SET status = 'active', failures = 0, last_error = NULL,"
                " fire_at = now() + interval '1 minute', updated_at = now()"
                " WHERE owner_id = :owner_id AND status = 'paused' AND"
            )
        elif status == "pause":
            payload_sql = (
                "UPDATE planning.watches SET status = 'paused', updated_at = now()"
                " WHERE owner_id = :owner_id AND status = 'active' AND"
            )
        else:
            payload_sql = (
                "UPDATE planning.watches SET status = 'cancelled', updated_at = now()"
                " WHERE owner_id = :owner_id AND status IN ('active', 'paused') AND"
            )
        sql = payload_sql + f" {where} RETURNING title, id::text AS id"
        async with self._session() as s:
            row = (
                (await s.execute(text(sql), {"owner_id": int(owner_id), "pat": ref[:120]}))
                .mappings()
                .first()
            )
            await s.commit()
        return None if row is None else f"{row['id']} → {status}: {row['title']}"

    async def claim_due(self, *, limit: int = 20) -> tuple[list[Watch], int]:
        """(созревшие ряды, сколько погасились по горизонту).

        Гашение «до horizons» идёт первым запросом той же транзакции: иначе «проверить просрочку»
        и «погасить просрочку» — два факта, между которыми ряд мог успеть выстрелить.
        """
        async with self._session() as s:
            expired_result = await s.execute(text(_EXPIRE_SQL))
            expired = int(expired_result.rowcount or 0)
            rows = (await s.execute(text(_CLAIM_SQL), {"limit": int(limit)})).mappings().all()
            await s.commit()
        return [_watch(r) for r in rows], expired

    async def finish(
        self,
        watch: Watch,
        *,
        hit: bool,
        digest: str,
        proof: str | None = None,
        quote: str | None = None,
        now: datetime,
        trace_id: str | None = None,
    ) -> tuple[str, str | None]:
        """Итог проверки одним коммитом: переворот наблюдения + (при попадании) напоминание.

        Атомарность — весь смысл: раздельные коммиты дали бы «условие учтено, а владелец не
        уведомлён» ровно в тот момент, когда процесс перезапускают посреди тика.
        """
        action = decide_after_check(
            hit,
            now=now,
            interval_minutes=watch.interval_minutes,
            expires_at=watch.expires_at,
            repeat=watch.repeat,
        )
        next_fire = now + timedelta(minutes=watch.interval_minutes)
        async with self._session() as s:
            reminder_id: str | None = None
            if hit:
                reminder_id = await insert_reminder(
                    s,
                    build_insert_params(
                        owner_id=watch.owner_id,
                        body=fire_text(watch, proof or "условие выполнено", quote or ""),
                        due_at=now,
                        trace_id=trace_id,
                        channel=watch.channel,
                    ),
                )
            # действие и состояние — разные слова: 'rearm' — что сделать, 'active' — чем стать
            status = {"fired": "fired", "expired": "expired"}.get(action, "active")
            await s.execute(
                text(
                    "UPDATE planning.watches SET status = :status, last_state = :last_state,"
                    " baseline_hash = :digest, last_error = NULL, failures = 0,"
                    " fire_at = :next_fire, updated_at = now() WHERE id = :id"
                ),
                {
                    "status": status,
                    "last_state": "yes" if hit else "no",
                    "digest": digest,
                    "next_fire": next_fire,
                    "id": watch.id,
                },
            )
            await s.commit()
        return action, reminder_id

    async def fail(self, watch: Watch, error: str, *, now: datetime) -> bool:
        """Ошибка проверки; после MAX_FAILURES подряд — пауза. True = поставили на паузу."""
        pause = watch_failures_left(watch) <= 1
        async with self._session() as s:
            await s.execute(
                text(
                    "UPDATE planning.watches SET failures = failures + 1, last_error = :err,"
                    " status = CASE WHEN :pause THEN 'paused' ELSE status END,"
                    " fire_at = :next_fire, updated_at = now() WHERE id = :id"
                ),
                {
                    "err": str(error)[:500],
                    "pause": pause,
                    "next_fire": now + timedelta(minutes=watch.interval_minutes),
                    "id": watch.id,
                },
            )
            await s.commit()
        return pause


def watch_failures_left(watch: Watch) -> int:
    return MAX_FAILURES - int(watch.failures)


async def run_watches(
    store: SqlWatchStore,
    *,
    page: WatchChecker | None = None,
    search: WatchChecker | None = None,
    limit: int = 20,
    now: datetime | None = None,
) -> WatchReport:
    """Один проход: забрать созревшие, проверить, переармовать. Отправка — не здесь (никогда)."""

    moment = now or datetime.now(UTC)
    report = WatchReport()
    due, report.expired = await store.claim_due(limit=limit)
    for watch in due:
        report.checked += 1
        checker = checker_for(watch, page=page, search=search)
        try:
            body = await checker.body(watch)
        except CheckError as exc:
            report.failed += 1
            paused = await store.fail(watch, str(exc), now=moment)
            report.paused += 1 if paused else 0
            report.notes.append(f"{watch.short_id}: проверка не удалась — {str(exc)[:120]}")
            continue
        hit, digest, proof = evaluate(watch.mode, watch.needle, body, watch.baseline_hash)
        quote = excerpt(body, watch.needle) if (hit and watch.mode == "contains") else None
        action, reminder_id = await store.finish(
            watch, hit=hit, digest=digest, proof=proof, quote=quote, now=moment
        )
        if action == "fired":
            report.fired += 1
            report.notes.append(
                f"{watch.short_id}: «{watch.title}» — {proof}, напоминание {str(reminder_id)[:8]}"
            )
            log.info("watch.fired", id=watch.short_id, title=watch.title, mode=watch.mode)
        elif action == "expired":
            report.expired += 1
            if hit:
                report.fired += 1  # последняя проверка успела попасть — напоминание создано
            report.notes.append(f"{watch.short_id}: «{watch.title}» — горизонт истёк")
        else:
            report.rearmed += 1
    if report.checked:
        log.info("watch.tick", **report.counts())
    return report
