"""Outbox-relay: перенос событий из таблицы в NATS JetStream (шаг 2).

Схема «событие и доменное изменение — одна транзакция» (ADR-004) гарантирует, что событие не
потеряется. Это — вторая половина: доставить его наружу так, чтобы «доставлено» значило «сервер
принял и сохранил», а не «мы отправили и забыли».

Правила, которые здесь защищаются:

* **at-least-once, не exactly-once.** Строка помечается опубликованной только после `ack` от
  JetStream. Упал между `ack` и `UPDATE` — событие уйдёт второй раз; потребитель дедуплицирует по
  `metadata.event_id`. Обратное (сначала пометить, потом послать) теряло бы события при каждом
  падении, и это единственный выбор, который здесь был;
* **порядок — внутри пакета.** `ORDER BY id` (bigserial) и публикация по одному: перестановка
  возможна между пакетами, но не внутри, и для потока одного владельца это несущественно;
* **исчерпанные попытки не крутятся вечно.** `attempts < OUTBOX_MAX_ATTEMPTS` отсекает строки на
  уровне выборки: иначе одно «ядовитое» событие (например, payload, который не принимает сервер)
  блокировало бы очередь и сжигало лимиты;
* **нет подписчика — не наша вина.** Это не ошибка ретрая: если NATS недоступен, строки остаются
  неопубликованными, тик возвращается с `stopped` и кодом 1, очередь догоняет сама.

Функции `subject_for` и `encode_event` — чистые и тестируются без SDK; `events/nats.py` — тонкий
адаптер, который обязан уметь только `connect / publish / close`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import orjson
import structlog

__all__ = [
    "NullTransport",
    "RelayReport",
    "RelayUnavailable",
    "Transport",
    "drain",
    "encode_event",
    "subject_for",
]

log = structlog.get_logger(__name__)

#: токен NATS-субъекта: без пробелов, без `.` и `*`/`>` — иначе субъект разъедется или станет
#: шаблоном, на который подпишутся чужие потоки
_TOKEN = re.compile(r"[^A-Za-z0-9_-]+")


class RelayUnavailable(RuntimeError):
    """Публиковать некуда: транспорт не настроен или не отвечает. Не «события плохие»."""


@runtime_checkable
class Transport(Protocol):
    """Узкий порт, который нужен relay'ю. Всё остальное — дело адаптера."""

    async def publish(self, subject: str, body: bytes) -> None: ...

    async def aclose(self) -> None: ...


def subject_for(stream_type: str, stream_id: str, event_type: str, *, prefix: str = "aegis") -> str:
    """Субъект события: `<prefix>.<тип потока>.<id потока>.<тип события>`.

    Токены вычищаются, а не экранируются: `owner:12345` обязан стать `owner-12345`, иначе id потока
    распадается на два уровня и подписка `aegis.owner.12345.>` начинает ловить чужие потоки.
    """
    parts = [prefix, stream_type, stream_id, event_type]
    return ".".join(_TOKEN.sub("-", str(part).strip()).strip("-") or "x" for part in parts)


def encode_event(row: dict[str, Any]) -> bytes:
    """Конверт события: полный, а не «только payload».

    `event_id` и `version` — то, по чему потребитель дедуплицирует повторную доставку и что стоит
    положить в заголовок `Nats-Msg-Id`; без них получатель вынужден сравнивать тела.
    """
    meta = row.get("metadata") or {}
    return orjson.dumps(
        {
            "event_id": meta.get("event_id") or f"outbox-{row['outbox_id']}",
            "stream_type": row.get("stream_type"),
            "stream_id": row.get("stream_id"),
            "version": row.get("version"),
            "type": row.get("event_type"),
            "occurred_at": meta.get("occurred_at"),
            "payload": row.get("payload") or {},
            "metadata": meta,
            "outbox_id": row.get("outbox_id"),
        }
    )


class NullTransport:
    """Никуда не публикует. Нужен для `--dry-run`: пробовать «что уйдёт» без транспорта.

    Без этого класса `aegis outbox tick --dry-run` требовал бы поднятый NATS — то есть «посмотреть
    очередь» было бы доступно только когда очередь уже можно чистить, что обесценивает пробу.
    """

    async def publish(self, subject: str, body: bytes) -> None:  # pragma: no cover - noop
        return None

    async def aclose(self) -> None:
        return None


def _events_phrase(count: int) -> str:
    """«1 событие ушло бы» / «2 события ушли бы» — существительное и глагол согласованы.

    Мелочь, которая обычно стоит отдельного бага в доверии к отчёту: «4 событий ушло бы» читается
    как машинный вывод, и дальше в него не смотрят.
    """
    mod10, mod100 = count % 10, count % 100
    if mod10 == 1 and mod100 != 11:
        return "событие ушло бы"
    if 2 <= mod10 <= 4 and not 12 <= mod100 <= 14:
        return "события ушли бы"
    return "событий ушло бы"


@dataclass(frozen=True, slots=True)
class RelayReport:
    """Итог тика. `stuck` — строки с исчерпанными попытками: их выборка больше не берёт."""

    published: int = 0
    failed: int = 0
    fetched: int = 0
    stuck: int = 0
    pending: int = 0
    dry_run: bool = False
    stopped: str | None = None

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def summary(self) -> str:
        if not self.fetched and not self.stuck and not self.pending:
            return "Неопубликованных событий нет."
        if self.dry_run:
            base = f"{self.fetched} {_events_phrase(self.fetched)} в NATS (ничего не опубликовано)"
        else:
            base = f"Опубликовано {self.published} из {self.fetched}"
        if self.pending:
            base += f" · в очереди ещё {self.pending}"
        if self.failed:
            base += f"; не доставлено: {self.failed} — останутся в очереди на следующий тик"
        if self.stuck:
            base += f"; ⚠️ {self.stuck} исчерпали попытки — нужна реакция"
        if self.stopped:
            base += f"\n  остановлено: {self.stopped}"
        return base


async def _counts(store: Any, max_attempts: int) -> tuple[int, int]:
    """(что ждёт публикации, что застряло). Магазин без счётчика — это (0, 0), без вранья."""
    counts = getattr(store, "counts", None)
    if counts is None:
        return 0, 0
    try:
        raw = await counts(max_attempts=max_attempts)
    except Exception as exc:  # noqa: BLE001 - отчёт не имеет права ронять тик
        log.warning("outbox.counts_failed", err=repr(exc)[:200])
        return 0, 0
    return int(raw.get("pending", 0)), int(raw.get("stuck", 0))


async def drain(
    store: Any,
    transport: Transport,
    *,
    limit: int = 50,
    max_attempts: int = 8,
    dry_run: bool = False,
    prefix: str = "aegis",
) -> RelayReport:
    """Один проход: выбрать непубликованное, отправить, пометить.

    `store` намеренно `Any`: relay работает с портом (`fetch_unpublished` / `mark_published` /
    `mark_failed`), и тесты подменяют его словарём-магазином, а `EventStore` подходит ему
    структурно.
    """
    pending, stuck = await _counts(store, max_attempts)
    rows: list[dict[str, Any]] = []
    if dry_run:
        peek = getattr(store, "peek_unpublished", None)
        if peek is not None:
            rows = list(await peek(limit=limit, max_attempts=max_attempts))
    else:
        rows = list(await store.fetch_unpublished(limit=limit, max_attempts=max_attempts))

    if dry_run:
        # ни публикации, ни отметок: «посмотреть, что ушло бы» не должно иметь ни одного
        # побочного эффекта — ни в брокере (событие уедет подписчикам дважды на следующем тике),
        # ни в счётчике попыток
        return RelayReport(
            published=0, failed=0, fetched=len(rows), stuck=stuck, pending=pending, dry_run=True
        )

    published_ids: list[int] = []
    stopped: str | None = None
    for row in rows:
        subject = subject_for(
            str(row.get("stream_type") or "event"),
            str(row.get("stream_id") or "?"),
            str(row.get("event_type") or "unknown"),
            prefix=prefix,
        )
        try:
            await transport.publish(subject, encode_event(row))
        except Exception as exc:  # noqa: BLE001 - транспорт лежит: остаток пакета не трогаем
            await _mark_failed(store, int(row["outbox_id"]), exc)
            stopped = f"публикация не удалась: {type(exc).__name__}: {str(exc)[:180]}"
            break
        published_ids.append(int(row["outbox_id"]))

    if published_ids:
        await store.mark_published(published_ids)
        # очередь считаем заново: «pending» в отчёте означает «осталось после тика», а не «было до»
        pending, stuck = await _counts(store, max_attempts)
    return RelayReport(
        published=len(published_ids),
        # не доставлено = всё, что не удалось отметить опубликованным: и упавшая строка, и остаток
        # пакета после обрыва — иначе «сбоев: 1» маскировало бы «тик простоял впустую»
        failed=len(rows) - len(published_ids),
        fetched=len(rows),
        stuck=stuck,
        pending=pending,
        dry_run=False,
        stopped=stopped,
    )


async def _mark_failed(store: Any, outbox_id: int, exc: BaseException) -> None:
    mark = getattr(store, "mark_failed", None)
    if mark is None:
        return
    try:
        await mark(outbox_id, f"{type(exc).__name__}: {str(exc)[:500]}")
    except Exception as inner:  # noqa: BLE001 - счётчик попыток важнее, чем его собственная ошибка
        log.warning("outbox.mark_failed_error", err=repr(inner)[:200])
