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
    "prepare_event",
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

    F4: `event_type` и `schema_version` — контрактные поля, по ним подписчик решает, какую
    payload-схему применять. Старые ключи (`type`, `metadata`) остаются как дубли:
    потребители шага 2 читают `type`, и «переименовали тихо» — breaking change,
    которого не будет.
    """
    meta = row.get("metadata") or {}
    return orjson.dumps(
        {
            "event_id": meta.get("event_id") or f"outbox-{row['outbox_id']}",
            "stream_type": row.get("stream_type"),
            "stream_id": row.get("stream_id"),
            "version": row.get("version"),
            "type": row.get("event_type"),
            "event_type": row.get("event_type"),
            "schema_version": int(row.get("schema_version") or 1),
            "occurred_at": meta.get("occurred_at"),
            "payload": row.get("payload") or {},
            "metadata": meta,
            "outbox_id": row.get("outbox_id"),
        }
    )


def _uuidish(value: str) -> str:
    """uuid есть uuid; нет — детерминированный uuid5 от исходного id.

    Дедупликация потребителя требует UUID-формата: «ev-12» или пустой id строки превращали бы
    каждый повтор доставки в «новый факт». Детерминизм важен: uuid5 от того же id одинаков на
    всех попытках — replay остаётся идемпотентным без реестра замен.
    """
    import uuid  # noqa: PLC0415

    text = (value or "").strip()
    try:
        return str(uuid.UUID(text))
    except ValueError:
        dns = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"  # NAMESPACE_DNS — стабильный якорь
        return str(uuid.uuid5(uuid.UUID(dns), f"aegis-outbox:{text}"))


def prepare_event(row: dict[str, Any]) -> tuple[bytes, list[str]]:
    """Собрать канонический конверт И проверить его о контракт. (body, errors).

    Проверка на публикации, а не на вставке: outbox пишет домен, и «контракт ужесточили —
    домен упал» переложил бы вину на стреляющего. Здесь же изоляция яда: невалидное событие
    не уходит (иначе отравленный подписчик вечно его переигрывает), а едет на DLQ-путь.

    Тело = канонический envelope + legacy-дубли (`type`, `metadata`, `outbox_id`): новые поля
    появляются, старые не исчезают — «тихий rename» и есть тот breaking change, от которого
    контракт защищает.
    """
    from aegis.platform.events.contracts import build_envelope, validate_envelope  # noqa: PLC0415

    legacy = orjson.loads(encode_event(row))
    meta = dict(legacy.get("metadata") or {})
    occurred_at = str(legacy.get("occurred_at") or "")
    if not occurred_at or occurred_at == "None":
        from datetime import UTC, datetime  # noqa: PLC0415

        occurred_at = datetime.now(UTC).isoformat()
    envelope = build_envelope(
        event_id=_uuidish(str(legacy.get("event_id") or "")),
        event_type=str(legacy.get("event_type") or legacy.get("type") or ""),
        schema_version=int(legacy.get("schema_version") or 1),
        occurred_at=occurred_at,
        stream_type=str(legacy.get("stream_type") or "event"),
        stream_id=str(legacy.get("stream_id") or "?"),
        version=int(legacy.get("version") or 1),
        payload=dict(legacy.get("payload") or {}),
        causation_id=meta.get("causation_id"),
    )
    errors = validate_envelope(envelope)
    body = orjson.dumps(envelope | {"metadata": meta, "outbox_id": legacy.get("outbox_id")})
    return body, errors


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
    dlq: int = 0
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
        if self.dlq:
            base += f"; в DLQ за этот тик: {self.dlq}"
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
    dlq_moves = 0
    stopped: str | None = None
    for row in rows:
        outbox_id = int(row["outbox_id"])
        try:
            body, contract_errors = prepare_event(row)
        except Exception as exc:  # noqa: BLE001 - тело без конверта — яд
            await _discard(
                store, outbox_id, f"envelope не собран: {type(exc).__name__}: {str(exc)[:200]}", row
            )
            continue
        if contract_errors:
            # контракт нарушен издателем: это не «попробовать ещё раз» — транспорт тут ни при чём.
            # Прямиком в DLQ, и очередь не стоит на этой строке (то, для чего max_attempts не
            # хватило: schema-ошибка не «отвалится» через 8 тиков)
            await _discard(
                store, outbox_id, "contract: " + "; ".join(contract_errors[:3])[:600], row
            )
            dlq_moves += 1
            continue
        subject = subject_for(
            str(row.get("stream_type") or "event"),
            str(row.get("stream_id") or "?"),
            str(row.get("event_type") or "unknown"),
            prefix=prefix,
        )
        try:
            await transport.publish(subject, body)
        except Exception as exc:  # noqa: BLE001 - транспорт лежит: остаток пакета не трогаем
            await _mark_failed(store, outbox_id, exc)
            attempts = int(row.get("attempts") or 0) + 1
            if attempts >= int(max_attempts):
                await _discard(store, outbox_id, f"исчерпаны попытки: {type(exc).__name__}", row)
                dlq_moves += 1
            stopped = f"публикация не удалась: {type(exc).__name__}: {str(exc)[:180]}"
            break
        published_ids.append(outbox_id)

    if published_ids:
        await store.mark_published(published_ids)
        # очередь считаем заново: «pending» в отчёте означает «осталось после тика», а не «было до»
        pending, stuck = await _counts(store, max_attempts)
    return RelayReport(
        published=len(published_ids),
        # не доставлено = всё, что не удалось отметить опубликованным (кроме осознанно
        # отбракованного в DLQ): и упавшая строка, и остаток пакета после обрыва — иначе «сбоев: 1»
        # маскировало бы «тик простоял впустую»
        failed=max(len(rows) - len(published_ids) - dlq_moves, 0),
        fetched=len(rows),
        stuck=stuck,
        pending=pending,
        dlq=dlq_moves,
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


async def _discard(store: Any, outbox_id: int, reason: str, row: dict[str, Any]) -> None:
    """В DLQ. Магазины без DLQ (тестовые двойники прошлой эпохи) получают только mark_failed:
    тихое «ничего» здесь хуже исключения — яд так и крутился бы в очереди. Поэтому, если ни
    move_to_dlq, ни mark_failed нет, ошибка наружу: relay обязан сказать, что не умеет
    утилизировать.
    """
    move = getattr(store, "move_to_dlq", None)
    if move is not None:
        await move(
            outbox_id, reason, {"row": row, "encoded": encode_event(row).decode("utf-8", "replace")}
        )
        log.warning("outbox.moved_to_dlq", outbox_id=outbox_id, reason=reason[:200])
        return
    mark = getattr(store, "mark_failed", None)
    if mark is None:
        raise RelayUnavailable(f"некуда деть отбракованное событие {outbox_id}: {reason[:200]}")
    await mark(outbox_id, reason)
    log.warning("outbox.rejected_without_dlq", outbox_id=outbox_id, reason=reason[:200])
