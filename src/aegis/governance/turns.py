"""Актив-актив (F1): открытый ход — строка в БД с fencing token, а не словарь процесса.

Почему это вообще отдельный слой. До сих пор всё держалось на единственности экземпляра:
``begin_turn``/``turn_step`` жили в памяти рекордера, счётчик шагов и «открытый ход» — тоже.
Два процесса на одной базе означали бы два независимых счёта шагов и два «активных хода» одного
владельца; рестарт процесса — потерю состояния. Здесь контракт параллельности становится явным:

* **аренда строки + fencing token.** ``begin`` вставляет заявку под advisory lock на trace_id и
  выдаёт монотонный токен из последовательности. Все операции над ходом (шаг, завершение)
  сверяют токен: процесс, у которого украли аренду (истёк lease), не может ни двигать шаги,
  ни закрыть чужой ход — это ровно тот «fencing», без которого двойной запуск молча портит
  журнал;
* **один ход на владельца, второй не теряется.** Пока у владельца активен ход, новый апдейт
  попадает в очередь ``governance.turn_queue`` (не отбрасывается!), и будет разобран тем процессом,
  который завершил текущий ход, либо ``aegis turns drain``;
* **всё восстанавливаемо из БД.** Промпт-версии, tools-schema и номер шага читаются из строки
  заявки: упавший бот после рестартa продолжает тот же ход с тем же номером шага.
  :class:`NullTurnLedger` — единственный честный способ работать без БД, и он кричит об этом
  (``durable=False`` видно в ``/status`` и в ``doctor``).

Что проверяет инварианты: ``tests/integration/test_turns.py`` — два «процесса» (две сессии) на
одной базе, 200 параллельных записей, «цепочка журнала цела, ход не исполнен дважды, ни один
апдейт не съеден».
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Protocol

import orjson
import structlog
from sqlalchemy import text

from aegis.platform.canonical import sha256_bytes
from aegis.platform.db import SessionFactory, session

__all__ = [
    "NullTurnLedger",
    "SqlTurnLedger",
    "TurnBusy",
    "TurnHandle",
    "TurnLedger",
    "TurnQueueItem",
]

log = structlog.get_logger(__name__)

#: advisory lock поверх uniqueness-индекса: вставка заявки сериализуется по trace_id, и два
#: процесса не получают возможность «оба первые». Ключ — хэш строки ``aegis:turn:{trace}``
_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('aegis:turn:' || CAST(:trace AS text)))"

#: «активен ли у владельца чужой ход» — не включая тот trace, который мы открываем повторно
_BUSY_CHECK = """
SELECT trace_id::text AS trace
  FROM governance.turn_claims
 WHERE owner_id = :owner_id AND status = 'active' AND lease_until >= now()
   AND trace_id <> CAST(:trace AS uuid)
 LIMIT 1
"""

#: просроченные аренды освобождаем точечно, а не «одним мусорщиком»: dropped остаётся в таблице
#: как факт («ход X был осиротевшим»), и это видно в ``aegis turns status``
_EXPIRE_CLAIM = """
UPDATE governance.turn_claims
   SET status = 'dropped', updated_at = now()
 WHERE owner_id = :owner_id AND status = 'active' AND lease_until < now()
"""

_EXISTING_CLAIM = """
SELECT fencing_token, status FROM governance.turn_claims WHERE trace_id = CAST(:trace AS uuid)
"""

#: повторный begin того же хода (resume после подтверждения, рестарт процесса): статус
#  восстанавливаем, шаг НЕ обнуляем — иначе журнал начнёт принимать один ход за два
_REACTIVATE_CLAIM = """
UPDATE governance.turn_claims
   SET status = 'active', finished_at = NULL, updated_at = now(),
       fencing_token = nextval('governance.fencing_seq'),
       lease_until = now() + make_interval(secs => :lease_secs),
       prompt_ids = CAST(:prompt_ids AS jsonb), tools_schema_sha = :tools_schema_sha
 WHERE trace_id = CAST(:trace AS uuid)
RETURNING fencing_token
"""

#: срок аренды подставляется параметром, а не интервалом-литералом: ``make_interval`` принимает
#: секунды, и тесты с коротким lease не вынуждены патчить SQL
_INSERT_CLAIM = """
INSERT INTO governance.turn_claims
    (trace_id, owner_id, fencing_token, prompt_ids, tools_schema_sha, lease_until)
VALUES (CAST(:trace AS uuid), :owner_id, nextval('governance.fencing_seq'),
        CAST(:prompt_ids AS jsonb), :tools_schema_sha,
        now() + make_interval(secs => :lease_secs))
RETURNING fencing_token
"""

_PEEK_CLAIM = """
SELECT owner_id, step, prompt_ids, tools_schema_sha, status
  FROM governance.turn_claims
 WHERE trace_id = CAST(:trace AS uuid)
"""

_LEASE_TOUCH = """
UPDATE governance.turn_claims
   SET lease_until = now() + make_interval(secs => :lease_secs), updated_at = now()
 WHERE trace_id = CAST(:trace AS uuid) AND status = 'active' AND fencing_token = :fence
RETURNING fencing_token
"""

_ADVANCE_STEP = """
UPDATE governance.turn_claims
   SET step = step + 1, updated_at = now(),
       lease_until = now() + make_interval(secs => :lease_secs)
 WHERE trace_id = CAST(:trace AS uuid) AND status = 'active' AND fencing_token = :fence
RETURNING step
"""

#: :func:`_reactivate_params` — из params begin'а для UPDATE-переактивации: только те ключи,
#: которые есть в SQL (owner_id там не нужен и не определён)


def _reactivate_params(params: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if k != "owner_id"}


_FINISH_CLAIM = """
UPDATE governance.turn_claims
   SET status = 'finished', finished_at = now(), updated_at = now()
 WHERE trace_id = CAST(:trace AS uuid) AND status = 'active' AND fencing_token = :fence
RETURNING fencing_token
"""

_ENQUEUE = """
INSERT INTO governance.turn_queue (owner_id, trace_id, kind, payload)
VALUES (:owner_id, CAST(:trace AS uuid), :kind, CAST(:payload AS jsonb))
"""

#: claimed = «drain забрал, ещё не довёл до финала»: статус нужен, чтобы два потребителя
#: не обработали одно сообщение дважды; осиротевшие claimed (крах процесса между pop и close)
#: возвращаются в игру теми же pop'ами, пока attempts не исчерпан
_POP_NEXT = """
WITH picked AS (
    SELECT id
      FROM governance.turn_queue
     WHERE owner_id = :owner_id
       AND (
           status = 'queued'
           OR (
               status = 'claimed'
               AND attempts < :max_attempts
               AND claimed_at < now() - make_interval(secs => :stale_secs)
           )
       )
     ORDER BY id
     FOR UPDATE SKIP LOCKED
     LIMIT 1
)
UPDATE governance.turn_queue AS q
   SET status = 'claimed', claimed_at = now(), attempts = q.attempts + 1
  FROM picked
 WHERE q.id = picked.id
RETURNING q.id, q.trace_id::text AS trace_id, q.kind, q.payload, q.attempts
"""

#: «пока не закроют» = максимум 3 попытки на сообщение; осиротевший claim — это тот, чей
#: владелец умер между pop и close: его возвращают в игру не раньше, чем через stale-окно,
#: иначе два живых потребителя видят одно сообщение дважды
_QUEUE_MAX_ATTEMPTS = 3
_QUEUE_CLAIM_STALE_SECS = 60

_CLOSE_ITEM = """
UPDATE governance.turn_queue SET status = 'done' WHERE id = :id
"""

_QUEUE_COUNT = """
SELECT count(*)::int FROM governance.turn_queue WHERE owner_id = :owner_id AND status = 'queued'
"""

_STATUS = """
SELECT
  count(*) FILTER (WHERE status = 'active') AS active,
  count(*) FILTER (WHERE status = 'active' AND lease_until < now()) AS expired,
  (SELECT count(*) FROM governance.turn_queue WHERE status = 'queued') AS queued
FROM governance.turn_claims
"""

#: сколько незакрытых заявок хранит Null-реализация — тот же потолок, что был у рекордера
_MAX_MEMORY_TURNS = 64


class TurnBusy(RuntimeError):
    """У владельца уже есть активный ход. Сигнал вызывающему коду: не отказать, а поставить в
    очередь."""

    def __init__(self, owner_id: int, active_trace: str | None = None) -> None:
        super().__init__(f"владелец {owner_id}: активен ход {active_trace or '?'}")
        self.owner_id = owner_id
        self.active_trace = active_trace


@dataclass(frozen=True, slots=True)
class TurnHandle:
    """Квитанция об открытии хода: без токена дальше ни шага, ни финала."""

    trace_id: str
    fencing_token: int
    owner_id: int
    #: durable=False — ход живёт только в этом процессе (нет БД). «После рестарта состояние
    #  потеряно» превращается из сюрприза в объявленное свойство конфигурации
    durable: bool = True

    def stale(self) -> bool:
        return not self.durable


@dataclass(slots=True)
class TurnQueueItem:
    queue_id: int
    trace_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 1


class TurnLedger(Protocol):
    """Порт «открытый ход». Реализации: Postgres (durable) и память (волатильно, но честно)."""

    durable: bool

    async def begin(
        self,
        trace_id: str,
        *,
        owner_id: int,
        prompt_ids: Sequence[Mapping[str, Any]] = (),
        tools_schema_sha: bytes | None = None,
    ) -> TurnHandle: ...

    async def peek(self, trace_id: str) -> dict[str, Any] | None: ...

    async def advance(self, trace_id: str, fencing_token: int | None = None) -> int: ...

    async def finish(self, trace_id: str, fencing_token: int | None = None) -> bool: ...

    async def enqueue(
        self, owner_id: int, *, trace_id: str, kind: str, payload: Mapping[str, Any]
    ) -> None: ...

    async def pop_next(self, owner_id: int) -> TurnQueueItem | None: ...

    async def close_item(self, queue_id: int) -> None: ...

    async def queue_count(self, owner_id: int) -> int: ...

    async def status(self) -> dict[str, int]: ...


class NullTurnLedger:
    """Однопроцессный режим с открытым забралом: ``durable=False`` видно отовсюду.

    Это не «младший брат» Sql-реализации, а объявление: состояние хода волатильно, рестарт его
    теряет, второй процесс о нём не узнает. Именно так и было всегда — разница в том, что теперь
    факт записан в контракте, а не вычитывается из чтения кода.
    """

    durable = False

    def __init__(self) -> None:
        self._turns: dict[str, dict[str, Any]] = {}
        self._queues: dict[int, list[TurnQueueItem]] = {}
        self._token = 0
        self._started = monotonic()

    async def begin(
        self,
        trace_id: str,
        *,
        owner_id: int,
        prompt_ids: Sequence[Mapping[str, Any]] = (),
        tools_schema_sha: bytes | None = None,
    ) -> TurnHandle:
        self._sweep()
        self._token += 1
        self._turns[trace_id] = {
            "owner_id": int(owner_id),
            "prompt_ids": [dict(item) for item in prompt_ids],
            "tools_schema_sha": tools_schema_sha,
            "step": 0,
        }
        return TurnHandle(
            trace_id=trace_id, fencing_token=self._token, owner_id=owner_id, durable=False
        )

    async def peek(self, trace_id: str) -> dict[str, Any] | None:
        turn = self._turns.get(trace_id)
        if turn is None:
            return None
        return {
            "owner_id": turn["owner_id"],
            "step": turn["step"],
            "prompt_ids": turn["prompt_ids"],
            "tools_schema_sha": turn["tools_schema_sha"],
            "status": "active",
        }

    async def advance(self, trace_id: str, fencing_token: int | None = None) -> int:
        turn = self._turns.get(trace_id)
        if turn is None:
            return 0
        turn["step"] = int(turn["step"]) + 1
        return int(turn["step"])

    async def finish(self, trace_id: str, fencing_token: int | None = None) -> bool:
        return self._turns.pop(trace_id, None) is not None

    async def enqueue(
        self, owner_id: int, *, trace_id: str, kind: str, payload: Mapping[str, Any]
    ) -> None:
        self._token += 1
        item = TurnQueueItem(
            queue_id=self._token,
            trace_id=trace_id,
            kind=kind,
            payload=dict(payload),
        )
        self._queues.setdefault(int(owner_id), []).append(item)

    async def pop_next(self, owner_id: int) -> TurnQueueItem | None:
        queue = self._queues.get(int(owner_id)) or []
        return queue.pop(0) if queue else None

    async def close_item(self, queue_id: int) -> None:
        return None

    async def queue_count(self, owner_id: int) -> int:
        return len(self._queues.get(int(owner_id)) or [])

    async def status(self) -> dict[str, int]:
        queued = sum(len(v) for v in self._queues.values())
        return {
            "active": len(self._turns),
            "expired": 0,
            "queued": queued,
            "durable": 0,
        }

    def _sweep(self) -> None:
        if len(self._turns) > _MAX_MEMORY_TURNS:
            # FIFO-вытеснение: ход без финала — это уже «упали между begin и end», держать его
            # дороже, чем потерять номер шага (цепочка журнала от этого не рвётся)
            for key in list(self._turns)[: len(self._turns) - _MAX_MEMORY_TURNS]:
                self._turns.pop(key, None)
            del self._started


class SqlTurnLedger:
    """Заявки хода в ``governance.turn_claims`` + очередь в ``governance.turn_queue``.

    Все методы работают своей транзакцией: заявка — не часть доменной записи, а координационный
    протокол, и откат домена не обязан (и не должен) отменять «ход открыт»: lease доедет до
    истечения и освободит владельца самостоятельно.
    """

    durable = True

    def __init__(
        self,
        *,
        session_factory: SessionFactory | None = None,
        lease_minutes: int = 15,
    ) -> None:
        self._sm = session_factory
        self._lease_secs = max(30, int(lease_minutes) * 60)

    def _session(self) -> Any:
        return self._sm() if self._sm is not None else session()

    async def begin(
        self,
        trace_id: str,
        *,
        owner_id: int,
        prompt_ids: Sequence[Mapping[str, Any]] = (),
        tools_schema_sha: bytes | None = None,
    ) -> TurnHandle:
        """Открыть (или вернуть) ход. Единственный метод, знающий про TurnBusy.

        Порядок внутри одной транзакции: advisory lock на trace → освобождение просроченных
        аренд владельца → проверка чужого активного хода → вставка. Уникальный частичный индекс
        ``(owner_id) WHERE status='active'`` — второй замок на случай гонки «проверил и вставил
        одновременно»: IntegrityError превращается в тот же TurnBusy, и ни один из двух процессов
        не считает себя победителем.
        """
        if not _is_uuid(trace_id):
            raise ValueError(f"trace_id должен быть uuid, получено {trace_id!r}")
        params = {
            "trace": trace_id,
            "owner_id": int(owner_id),
            "prompt_ids": orjson.dumps([dict(item) for item in prompt_ids]).decode(),
            "tools_schema_sha": tools_schema_sha,
            "lease_secs": self._lease_secs,
        }
        from sqlalchemy.exc import IntegrityError  # noqa: PLC0415 — только на пути гонки

        try:
            async with self._session() as s:
                await s.execute(text(_LOCK_SQL), {"trace": trace_id})
                await s.execute(text(_EXPIRE_CLAIM).bindparams(owner_id=int(owner_id)))
                # явно два параметра: у проверки занятости нет ни prompt_ids, ни lease —
                # «bindparams всем словарём» упало бы «параметр не определён» вместо ответа
                busy = await s.scalar(
                    text(_BUSY_CHECK).bindparams(owner_id=int(owner_id), trace=trace_id)
                )
                if busy:
                    raise TurnBusy(int(owner_id), str(busy))
                existing = (
                    (await s.execute(text(_EXISTING_CLAIM).bindparams(trace=trace_id)))
                    .mappings()
                    .first()
                )
                if existing is not None and existing["status"] != "active":
                    # owner у переактивации не переставляется, и в UPDATE его параметра нет:
                    # «bindparams всем словарём» здесь — источник ArgumentError вместо хода
                    token = await s.scalar(
                        text(_REACTIVATE_CLAIM).bindparams(**_reactivate_params(params))
                    )
                elif existing is not None:
                    token = await s.scalar(
                        text(_LEASE_TOUCH).bindparams(
                            trace=trace_id,
                            fence=int(existing["fencing_token"]),
                            lease_secs=self._lease_secs,
                        )
                    )
                    if token is None:  # «активная» строка успела закрыться — восстанавливаем
                        token = await s.scalar(
                            text(_REACTIVATE_CLAIM).bindparams(**_reactivate_params(params))
                        )
                else:
                    token = await s.scalar(text(_INSERT_CLAIM).bindparams(**params))
                await s.commit()
        except IntegrityError as exc:
            raise TurnBusy(int(owner_id)) from exc
        return TurnHandle(trace_id=trace_id, fencing_token=int(token or 0), owner_id=int(owner_id))

    async def peek(self, trace_id: str) -> dict[str, Any] | None:
        if not _is_uuid(trace_id):
            return None
        async with self._session() as s:
            row = (await s.execute(text(_PEEK_CLAIM).bindparams(trace=trace_id))).mappings().first()
        if row is None:
            return None
        data = dict(row)
        data["prompt_ids"] = data.get("prompt_ids") or []
        return data

    async def advance(self, trace_id: str, fencing_token: int | None = None) -> int:
        """Следующий номер шага; ``0`` — ход закрыт, чужой или просрочен (fencing не пустил).

        Молча вернуть 0 — осознанный выбор вместо исключения: шаг нужен для подписи записей
        журнала, и старый процесс должен дописать свой ответ, но не имеет права двигать счётчик
        чужого хода. Потерянный шаг («0 = не размечен») журнал переживает; исключение убило бы
        ответ владельцу.
        """
        if not _is_uuid(trace_id) or fencing_token is None:
            return 0
        async with self._session() as s:
            step = await s.scalar(
                text(_ADVANCE_STEP).bindparams(
                    trace=trace_id, fence=int(fencing_token), lease_secs=self._lease_secs
                )
            )
            await s.commit()
        return int(step) if step is not None else 0

    async def finish(self, trace_id: str, fencing_token: int | None = None) -> bool:
        if not _is_uuid(trace_id):
            return False
        if fencing_token is None:
            # без токена финал всё равно возможен — но только по «последнему активному»: это
            # путь CLI-расчистки, а не ходового кода, и он помечен в логе
            async with self._session() as s:
                closed = await s.scalar(
                    text(
                        "UPDATE governance.turn_claims SET status='finished', finished_at=now(),"
                        " updated_at=now() WHERE trace_id = CAST(:trace AS uuid)"
                        " AND status='active' RETURNING fencing_token"
                    ).bindparams(trace=trace_id)
                )
                await s.commit()
            log.info("turns.finish_unfenced", trace_id=trace_id)
            return closed is not None
        async with self._session() as s:
            done = await s.scalar(
                text(_FINISH_CLAIM).bindparams(trace=trace_id, fence=int(fencing_token))
            )
            await s.commit()
        return done is not None

    async def enqueue(
        self, owner_id: int, *, trace_id: str, kind: str, payload: Mapping[str, Any]
    ) -> None:
        async with self._session() as s:
            await s.execute(
                text(_ENQUEUE).bindparams(
                    owner_id=int(owner_id),
                    trace=trace_id if _is_uuid(trace_id) else str(uuid.uuid4()),
                    kind=kind[:40],
                    payload=orjson.dumps(dict(payload)).decode(),
                )
            )
            await s.commit()

    async def pop_next(self, owner_id: int) -> TurnQueueItem | None:
        async with self._session() as s:
            row = (
                (
                    await s.execute(
                        text(_POP_NEXT).bindparams(
                            owner_id=int(owner_id),
                            max_attempts=_QUEUE_MAX_ATTEMPTS,
                            stale_secs=_QUEUE_CLAIM_STALE_SECS,
                        )
                    )
                )
                .mappings()
                .first()
            )
            await s.commit()
        if row is None:
            return None
        return TurnQueueItem(
            queue_id=int(row["id"]),
            trace_id=str(row["trace_id"]),
            kind=str(row["kind"]),
            payload=dict(row["payload"] or {}),
            attempts=int(row["attempts"]),
        )

    async def close_item(self, queue_id: int) -> None:
        async with self._session() as s:
            await s.execute(text(_CLOSE_ITEM).bindparams(id=int(queue_id)))
            await s.commit()

    async def queue_count(self, owner_id: int) -> int:
        async with self._session() as s:
            return int(await s.scalar(text(_QUEUE_COUNT).bindparams(owner_id=int(owner_id))) or 0)

    async def status(self) -> dict[str, int]:
        try:
            async with self._session() as s:
                row = (await s.execute(text(_STATUS))).mappings().first()
        except Exception as exc:  # noqa: BLE001 — status не имеет права ронять /status
            log.warning("turns.status_failed", err=repr(exc)[:200])
            return {"error": 1}
        data = {key: int(value or 0) for key, value in dict(row or {}).items()}
        data["durable"] = 1
        return data


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def trace_for_text(text_value: str, owner_id: int) -> str:
    """Детерминированный trace_id для текстового апдейта (dedup-ключ второго шанса).

    Повторная доставка того же апдейта Telegram обязана попадать в тот же trace: иначе
    «не съесть апдейт» превращается в «исполнить его дважды под разными именами». Хэш считается
    от (chat, update_id) в первую очередь; эта функция — для путей без update_id (CLI ask).
    """
    seed = orjson.dumps({"text": text_value, "owner": int(owner_id)})
    return str(uuid.UUID(bytes=sha256_bytes(b"aegis:trace:" + seed)[:16]))
