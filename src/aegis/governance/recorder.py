"""Журнал решений (M1): точный вход, точный выход, целостность без доверия к себе.

Зачем это вообще нужно. ``platform.llm_calls`` и ``governance.tool_runs`` уже пишут «какая модель,
сколько токенов, какое решение policy». На них держится отчётность, но не ответ на вопрос «почему ты
так ответил — и докажи». Для этого нужны три вещи, которых в аудите нет:

1. **Точный вход.** Не «сообщения, наверное, были такие», а байты запроса: собранный промпт после
   DLP, схемы инструментов, параметры модели. Хранятся контент-адресно (``platform.blobs``), поэтому
   одинаковые префиксы не размножаются, а запись несёт 32 байта ссылки вместо пересказа.
2. **Целостность.** ``prev_hash → hash`` по всему журналу: проверить «историю не правили» может
   кто угодно с доступом к БД, без доступа к приложению и к его хорошему намерению.
3. **Воспроизводимость.** По записи собирается тот же запрос и прогоняется с замороженными
   результатами инструментов (:mod:`aegis.governance.replay`).

Два правила, от которых зависит, будет это доказательством или косметикой:

* журнал append-only на уровне БД (миграция ``0002``), и цепочку мы не «чиним» задним числом: обрыв
  — это факт, который надо показать владельцу, а не загладить;
* сбой записи не имеет права отнять у владельца ответ. Пишем best-effort, но не молчим:
  :attr:`failures` виден в ``/status`` и в ``aegis doctor``, потому что потерянная трасса — это
  потерянное доказательство, а не «чуть меньше метрик».
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Literal, Protocol

import orjson
import structlog
from sqlalchemy import text

from aegis.platform.canonical import canonical_bytes, sha256_bytes
from aegis.platform.config import Settings, settings
from aegis.platform.db import SessionFactory, session
from aegis.platform.gateway.diagnose import scrub_secrets

__all__ = [
    "AnchorReport",
    "BlobRef",
    "BlobStore",
    "ChainReport",
    "DecisionRecorder",
    "NullDecisionRecorder",
    "SqlDecisionRecorder",
    "check_chain",
    "link_hash",
    "merkle_root",
    "record_payload",
]

log = structlog.get_logger(__name__)

#: общий advisory lock: цепочка обязана быть линейной даже при параллельных ходах
_LOCK_KEY = 4242
_HASH_LEN = 32
_JSON = "application/json"
#: поля, из которых считается хэш. `seq` и `created_at` вне цепочки: первое — состояние БД,
#: второе поменялось бы при любой сверке в другом часовом поясе.
_HASHED_FIELDS: tuple[str, ...] = (
    "id",
    "trace_id",
    "turn_no",
    "kind",
    "owner_id",
    "prompt_ids",
    "tools_schema_sha",
    "model",
    "params",
    "input_sha",
    "output_sha",
    "policy",
    "cost_usd",
    "latency_ms",
    "truncated",
    "note",
    "prev_hash",
)

RecordKind = Literal["llm_call", "tool_run", "policy", "turn_summary", "verdict"]

#: Сколько незакрытых ходов держим в памяти для корреляции «вызов модели ↔ шаг хода». Личный бот
#: столько не открывает одновременно, но утечка из-за одного неудачного `finally` была бы хуже.
_MAX_OPEN_TURNS = 64
#: Незакрытый ход старше этого — мусор (упали между begin и end): выкидываем при следующем begin.
_TURN_STALE_S = 900.0


def _money(value: Any) -> str:
    """Стоимость в цепочке — строка с фиксированным масштабом.

    Иначе ``numeric(12,6)`` из Postgres и ``0.0`` из Python дали бы разные байты, и цепочка рвалась
    бы на собственных записях: проверка падает не потому, что историю правили, а потому,
    что проверка и запись считают канон по-разному.
    """
    if value is None:
        return "0.000000"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"raw:{str(value)[:64]}"
    return f"{number:.6f}"


def _sha_bytes(value: Any) -> bytes | None:
    """Привести хэш к 32 байтам: из hex-строки, из bytes, из «чего-то похожего» — через sha256."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return raw if len(raw) == _HASH_LEN else sha256_bytes(raw)[:_HASH_LEN]
    text_value = str(value)
    try:
        decoded = bytes.fromhex(text_value)
    except ValueError:
        return sha256_bytes(text_value.encode("utf-8"))[:_HASH_LEN]
    return decoded if len(decoded) == _HASH_LEN else sha256_bytes(decoded)[:_HASH_LEN]


def record_payload(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Канонический вид записи для хэширования: только осмысленные поля, ничего «системного»."""
    out: dict[str, Any] = {}
    for key in _HASHED_FIELDS:
        value = fields.get(key)
        if key == "cost_usd":
            out[key] = _money(value)
        elif key in ("tools_schema_sha", "input_sha", "output_sha", "prev_hash"):
            raw = _sha_bytes(value)
            out[key] = raw.hex() if raw else None
        elif key == "prompt_ids":
            out[key] = [dict(item) for item in (value or [])]
        else:
            out[key] = value
    return out


def link_hash(prev_hash: bytes, payload: Mapping[str, Any]) -> bytes:
    """sha256(prev_hash || канон(записи)). Ровно это пересчитывает `verify` — без магии."""
    body = {key: value for key, value in payload.items() if key != "hash"}
    return sha256_bytes(bytes(prev_hash) + canonical_bytes(record_payload(body)))


def merkle_root(leaves: Sequence[bytes]) -> bytes:
    """Корень над хэшами записей дня: 32 байта, которыми можно прикрыть весь день одним числом."""
    level = [bytes(leaf) for leaf in leaves]
    if not level:
        return sha256_bytes(b"aegis:empty-day")
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])  # нечётный хвост дублируем: детерминированно и без потерь
        level = [
            sha256_bytes(left + right) for left, right in zip(level[::2], level[1::2], strict=True)
        ]
    return level[0]


@dataclass(frozen=True, slots=True)
class BlobRef:
    """Ссылка на содержимое: хэш, реальный размер и честно признанное усечение."""

    sha256: bytes
    size: int
    truncated: bool = False

    @property
    def hex(self) -> str:
        return self.sha256.hex()


@dataclass(slots=True)
class ChainReport:
    """Итог сверки цепочки. ``ok`` — только когда нет ни расхождений хэшей, ни пропусков."""

    ok: bool
    checked: int = 0
    first_seq: int | None = None
    last_seq: int | None = None
    problems: list[str] = field(default_factory=list)
    gaps: int = 0
    missing_blobs: int = 0
    truncated: int = 0

    def summary(self) -> str:
        if not self.checked:
            return "записей нет — воспроизводить нечего"
        head = f"проверено {self.checked} записей (seq {self.first_seq}–{self.last_seq})"
        if self.ok and not self.gaps and not self.missing_blobs:
            return f"{head}: цепочка цела"
        bits = [head]
        if self.gaps:
            bits.append(f"пропусков в порядковых номерах: {self.gaps} (кто-то удалял строки)")
        if self.missing_blobs:
            bits.append(
                f"ссылок на утраченное содержимое: {self.missing_blobs} "
                "(ход воспроизводим только по хэшам)"
            )
        if self.truncated:
            bits.append(f"записей с усечённым содержимым: {self.truncated}")
        bits.extend(self.problems[:6])
        if len(self.problems) > 6:
            bits.append(f"…и ещё {len(self.problems) - 6} расхождений")
        return "\n".join(bits)


@dataclass(slots=True)
class AnchorReport:
    ok: bool
    day: str
    records: int = 0
    merkle_root: str = ""
    first_seq: int = 0
    last_seq: int = 0
    note: str = ""


class DecisionRecorder(Protocol):
    """Порт для supervisor'а и шлюза: писать можно всегда, даже когда писать некуда."""

    failures: int

    @property
    def enabled(self) -> bool: ...

    def begin_turn(
        self,
        trace_id: str,
        *,
        owner_id: int,
        prompt_ids: Sequence[Mapping[str, Any]] = (),
        tools_schema_sha: bytes | None = None,
    ) -> None: ...

    def end_turn(self, trace_id: str) -> None: ...

    def turn_step(self, trace_id: str) -> int: ...

    async def on_llm_call(
        self,
        record: Any,
        *,
        owner_id: int = 0,
        turn_no: int | None = None,
        prompt_ids: Sequence[Mapping[str, Any]] = (),
        tools_schema_sha: bytes | None = None,
    ) -> None: ...

    async def tool_run(
        self,
        *,
        trace_id: str,
        turn_no: int,
        owner_id: int,
        tool: str,
        args: Mapping[str, Any],
        result: str,
        trust: str,
        decision: str,
        ok: bool,
        latency_ms: int = 0,
    ) -> None: ...

    async def policy(
        self,
        *,
        trace_id: str,
        turn_no: int,
        owner_id: int,
        tool: str,
        decision: str,
        reason: str,
        risk: str = "",
    ) -> None: ...

    async def verdict(
        self,
        *,
        trace_id: str,
        turn_no: int,
        owner_id: int,
        ok: bool,
        severity: str = "none",
        checked: Sequence[str] = (),
        problems: Sequence[str] = (),
        model: str | None = None,
        cost_usd: float = 0.0,
        latency_ms: int = 0,
        prompt_ids: Sequence[Mapping[str, Any]] = (),
        payload: Mapping[str, Any] | None = None,
    ) -> None: ...

    async def turn_summary(
        self,
        *,
        trace_id: str,
        turn_no: int,
        owner_id: int,
        messages: Sequence[Mapping[str, Any]],
        answer: str,
        model: str | None,
        cost_usd: float,
        latency_ms: int,
        iterations: int,
        prompt_ids: Sequence[Mapping[str, Any]] = (),
        tools_schema_sha: bytes | None = None,
        route: str = "",
        notes: Sequence[str] = (),
    ) -> None: ...

    async def verify(
        self, *, since: str | None = None, until: str | None = None, limit: int = 5000
    ) -> ChainReport: ...

    async def anchor(self, day: str | None = None) -> AnchorReport: ...

    async def records_for_trace(self, trace_id: str) -> list[dict[str, Any]]: ...

    async def latest_trace(self, *, owner_id: int) -> str | None: ...

    async def matching_traces(self, ref: str, *, limit: int = 3) -> list[str]: ...

    async def record_input(self, record: Mapping[str, Any]) -> Any: ...

    async def stats(self) -> dict[str, Any]: ...


class NullDecisionRecorder:
    """Без БД воспроизводимость физически невозможна — и это надо сказать, а не сделать вид."""

    def __init__(self) -> None:
        self.failures = 0

    @property
    def enabled(self) -> bool:
        return False

    def begin_turn(self, *args: Any, **kwargs: Any) -> None:
        return None

    def end_turn(self, *args: Any, **kwargs: Any) -> None:
        return None

    def turn_step(self, trace_id: str) -> int:
        return 0

    async def on_llm_call(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def tool_run(self, **kwargs: Any) -> None:
        return None

    async def policy(self, **kwargs: Any) -> None:
        return None

    async def verdict(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def turn_summary(self, **kwargs: Any) -> None:
        return None

    async def verify(self, **kwargs: Any) -> ChainReport:
        return ChainReport(
            ok=False, problems=["воспроизводимость выключена: нет БД, куда писать журнал"]
        )

    async def anchor(self, day: str | None = None) -> AnchorReport:
        return AnchorReport(ok=False, day=day or "", note="воспроизводимость выключена: нет БД")

    async def records_for_trace(self, trace_id: str) -> list[dict[str, Any]]:
        return []

    async def latest_trace(self, *, owner_id: int) -> str | None:
        return None

    async def matching_traces(self, ref: str, *, limit: int = 3) -> list[str]:
        return []

    async def record_input(self, record: Mapping[str, Any]) -> Any:
        return None

    async def stats(self) -> dict[str, Any]:
        return {"enabled": False, "records": 0, "blobs": 0, "blob_bytes": 0, "anchors": 0}


class SqlDecisionRecorder:
    """Запись в ``governance.decision_records`` + ``platform.blobs``, сверка цепочки, якорь дня.

    Репозиторий, а не сервис: никаких знаний о том, что такое «ход агента». Supervisor вызывает его
    точечно и не может сломать ответ владельца — ошибки записью в ``failures`` и всё.
    """

    def __init__(
        self,
        cfg: Settings | None = None,
        *,
        session_factory: SessionFactory | None = None,
        blobs: BlobStore | None = None,
    ) -> None:
        self.cfg = cfg or settings()
        self._sm = session_factory
        self.blobs = blobs or BlobStore(session_factory, max_bytes=self.cfg.repro_max_blob_bytes)
        self.failures = 0
        #: открытые ходы: trace_id → {owner_id, prompt_ids, tools_schema_sha, step, opened_at}.
        #  Шлюз зовёт on_llm_call и не знает ни владельца, ни версии промпта: эти вещи живут здесь,
        #  иначе корреляция «шаг хода ↔ вызов модели» свелась бы к угадыванию по времени.
        self._turns: dict[str, dict[str, Any]] = {}

    @property
    def enabled(self) -> bool:
        return True

    # ------------------------------------------------ ход (корреляция вызовов модели)

    def begin_turn(
        self,
        trace_id: str,
        *,
        owner_id: int,
        prompt_ids: Sequence[Mapping[str, Any]] = (),
        tools_schema_sha: bytes | None = None,
    ) -> None:
        if not _is_uuid(trace_id):
            return
        self._sweep_turns()
        self._turns[trace_id] = {
            "owner_id": int(owner_id),
            "prompt_ids": [dict(item) for item in prompt_ids],
            "tools_schema_sha": tools_schema_sha,
            "step": 0,
            "opened_at": monotonic(),
        }

    def end_turn(self, trace_id: str) -> None:
        self._turns.pop(trace_id, None)

    def turn_step(self, trace_id: str) -> int:
        """Начать новый шаг хода и вернуть его номер: 1, 2, 3 …

        Нужен, чтобы ``llm_call``, ``policy`` и ``tool_run`` одного хода читались как
        последовательность, а не как три кучи с общим trace_id. Нумерация с единицы: ``0`` в журнале
        означает «шаг не размечен» (ход без begin_turn), и смешивать это с первым шагом нельзя.
        """
        turn = self._turns.get(trace_id)
        if turn is None:
            return 0
        step = int(turn["step"]) + 1
        turn["step"] = step
        return step

    def _sweep_turns(self) -> None:
        now = monotonic()
        stale = [
            key
            for key, turn in self._turns.items()
            if now - float(turn.get("opened_at", now)) > _TURN_STALE_S
        ]
        for key in stale:
            self._turns.pop(key, None)
        while len(self._turns) > _MAX_OPEN_TURNS:
            self._turns.pop(next(iter(self._turns)))

    # ------------------------------------------------ запись

    async def on_llm_call(
        self,
        record: Any,
        *,
        owner_id: int = 0,
        turn_no: int | None = None,
        prompt_ids: Sequence[Mapping[str, Any]] = (),
        tools_schema_sha: bytes | None = None,
    ) -> None:
        """Точный запрос и точный ответ модели.

        Берём ``request``/``response`` шлюза: в них ровно то, что ушло в API (после DLP-маскировки),
        а не то, как нам казалось при сборке.
        """
        trace = str(getattr(record, "trace_id", "") or "")
        if not _is_uuid(trace):
            return
        open_turn = self._turns.get(trace) or {}
        owner_id = int(open_turn.get("owner_id") or owner_id)
        if turn_no is None:
            turn_no = int(open_turn.get("step", 0) or 0)
        if not prompt_ids:
            prompt_ids = list(open_turn.get("prompt_ids") or [])
        if tools_schema_sha is None:
            tools_schema_sha = open_turn.get("tools_schema_sha")
        request = getattr(record, "request", None)
        response = getattr(record, "response", None)
        if request is None and response is None:
            return  # шлюз писал метрики без содержимого: выключен repro_record_payload
        payload: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "trace_id": trace,
            "turn_no": int(turn_no),
            "kind": "llm_call",
            "owner_id": int(owner_id),
            "prompt_ids": [dict(item) for item in prompt_ids],
            "tools_schema_sha": _sha_bytes(tools_schema_sha),
            "model": getattr(record, "model", None),
            "params": {
                "role": getattr(record, "role", ""),
                "provider": getattr(record, "provider", "primary"),
                "attempt": int(getattr(record, "attempt", 0) or 0),
                "ok": bool(getattr(record, "ok", True)),
                "prompt_tokens": int(getattr(record, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(record, "completion_tokens", 0) or 0),
            },
            "policy": None,
            "cost_usd": _money(getattr(record, "cost_usd", 0.0)),
            "latency_ms": int(getattr(record, "latency_ms", 0) or 0),
            "truncated": False,
            "note": (getattr(record, "error", None) or "")[:400] or None,
        }
        try:
            refs_in = await self.blobs.put(request if request is not None else {})
            refs_out = await self.blobs.put(response if response is not None else {})
        except Exception as exc:  # noqa: BLE001 - журнал не имеет права ронять ответ владельцу
            self._fail("blob", exc, trace=trace)
            return
        payload["input_sha"] = refs_in.sha256
        payload["output_sha"] = refs_out.sha256
        payload["truncated"] = refs_in.truncated or refs_out.truncated
        await self._append(payload)

    async def tool_run(
        self,
        *,
        trace_id: str,
        turn_no: int,
        owner_id: int,
        tool: str,
        args: Mapping[str, Any],
        result: str,
        trust: str,
        decision: str,
        ok: bool,
        latency_ms: int = 0,
    ) -> None:
        """Вызов инструмента: аргументы, результат и доверие к нему — всё отдельно от пересказа."""
        if not _is_uuid(trace_id):
            return
        args_bytes = orjson.dumps(dict(args), default=str)
        payload: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "trace_id": trace_id,
            "turn_no": int(turn_no),
            "kind": "tool_run",
            "owner_id": int(owner_id),
            "prompt_ids": [],
            "tools_schema_sha": None,
            "model": None,
            "params": {
                "tool": tool,
                "trust": trust,
                "ok": bool(ok),
                "args_sha": sha256_bytes(scrub_secrets(args_bytes.decode()).encode()).hex(),
            },
            "policy": {"decision": decision},
            "cost_usd": "0.000000",
            "latency_ms": int(latency_ms),
            "truncated": False,
            "note": None,
        }
        # в аргументах и результатах теоретически что угодно, вплоть до содержимого файла:
        # секреты провайдера вычищаем до хэша, PII здесь уже закрыт DLP на входе в промпт
        blob_in = await self.blobs.put(scrub_secrets(args_bytes.decode()), media_type="text/plain")
        blob_out = await self.blobs.put(scrub_secrets(result or ""), media_type="text/plain")
        payload["input_sha"] = blob_in.sha256
        payload["output_sha"] = blob_out.sha256
        payload["truncated"] = blob_in.truncated or blob_out.truncated
        await self._append(payload)

    async def policy(
        self,
        *,
        trace_id: str,
        turn_no: int,
        owner_id: int,
        tool: str,
        decision: str,
        reason: str,
        risk: str = "",
    ) -> None:
        """Решение политики — записью, а не строкой в логе: «кто решил» должно читаться годами."""
        if not _is_uuid(trace_id):
            return
        await self._append(
            {
                "id": str(uuid.uuid4()),
                "trace_id": trace_id,
                "turn_no": int(turn_no),
                "kind": "policy",
                "owner_id": int(owner_id),
                "prompt_ids": [],
                "tools_schema_sha": None,
                "model": None,
                "params": {"tool": tool, "risk": risk},
                "policy": {"decision": decision, "reason": reason[:600]},
                "cost_usd": "0.000000",
                "latency_ms": 0,
                "truncated": False,
                "note": None,
            }
        )

    async def verdict(
        self,
        *,
        trace_id: str,
        turn_no: int,
        owner_id: int,
        ok: bool,
        severity: str = "none",
        checked: Sequence[str] = (),
        problems: Sequence[str] = (),
        model: str | None = None,
        cost_usd: float = 0.0,
        latency_ms: int = 0,
        prompt_ids: Sequence[Mapping[str, Any]] = (),
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        """Вердикт верификатора — отдельной записью, а не припиской к чужой.

        «Проверено и сошлось» и «проверять было нечем» обязаны различаться одним запросом: иначе
        через полгода журнал не скажет, был ли у ответа шанс оказаться выдумкой. Что именно сверяли
        (вопрос, ответ, источники) уходит в блоб, поэтому запись читается по факту, а не только по
        хэшу. Колонка ``policy`` держит итог: ``ok`` — отвечаем как есть, ``flagged`` —
        предупреждаем владельца.
        """
        if not _is_uuid(trace_id):
            return
        record: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "trace_id": trace_id,
            "turn_no": int(turn_no),
            "kind": "verdict",
            "owner_id": int(owner_id),
            # промпт судьи — здесь: «чем мерили» относится к записи так же, как «чем вооружён ход»
            "prompt_ids": [dict(item) for item in prompt_ids],
            "tools_schema_sha": None,
            "model": model,
            "params": {"severity": severity, "checked": [c[:120] for c in checked[:12]]},
            "policy": {
                "decision": "ok" if ok else "flagged",
                "reason": "; ".join(item[:200] for item in problems[:6])[:600] or "расхождений нет",
            },
            "cost_usd": _money(cost_usd),
            "latency_ms": int(latency_ms),
            "truncated": False,
            "note": None,
        }
        if payload is not None:
            blob = await self.blobs.put(dict(payload))
            record["input_sha"] = blob.sha256
            record["truncated"] = blob.truncated
        await self._append(record)

    async def turn_summary(
        self,
        *,
        trace_id: str,
        turn_no: int,
        owner_id: int,
        messages: Sequence[Mapping[str, Any]],
        answer: str,
        model: str | None,
        cost_usd: float,
        latency_ms: int,
        iterations: int,
        prompt_ids: Sequence[Mapping[str, Any]] = (),
        tools_schema_sha: bytes | None = None,
        route: str = "",
        notes: Sequence[str] = (),
    ) -> None:
        """Итог хода: собранный вход целиком и то, что ответили. Это и есть «воспроизведи меня»."""
        if not _is_uuid(trace_id):
            return
        payload: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "trace_id": trace_id,
            "turn_no": int(turn_no),
            "kind": "turn_summary",
            "owner_id": int(owner_id),
            "prompt_ids": [dict(item) for item in prompt_ids],
            "tools_schema_sha": _sha_bytes(tools_schema_sha),
            "model": model,
            "params": {"iterations": int(iterations), "route": route},
            "policy": None,
            "cost_usd": _money(cost_usd),
            "latency_ms": int(latency_ms),
            "truncated": False,
            "note": "; ".join(note for note in notes if note)[:600] or None,
        }
        blob_in = await self.blobs.put([dict(message) for message in messages])
        blob_out = await self.blobs.put(answer or "", media_type="text/plain")
        payload["input_sha"] = blob_in.sha256
        payload["output_sha"] = blob_out.sha256
        payload["truncated"] = blob_in.truncated or blob_out.truncated
        await self._append(payload)

    async def _append(self, payload: dict[str, Any]) -> None:
        """Один шаг цепочки: взять хвост под lock, досчитать хэш, вставить. Всё или ничего.

        Транзакция одна на запись и она же держит advisory lock: иначе два одновременных хода
        прочитали бы один и тот же ``prev_hash``, и журнал превратился бы в дерево — «цепочка»
        формально целая, а порядок записей уже не восстановишь.
        """
        try:
            async with self._session() as s:
                await s.execute(text(f"SELECT pg_advisory_xact_lock({_LOCK_KEY})"))
                prev = await s.scalar(
                    text("SELECT hash FROM governance.decision_records ORDER BY seq DESC LIMIT 1")
                )
                prev_hash = bytes(prev) if prev else bytes(_HASH_LEN)
                record = {**payload, "prev_hash": prev_hash}
                digest = link_hash(prev_hash, record)
                await s.execute(text(_INSERT_RECORD), _insert_params(record, digest))
                await s.commit()
        except Exception as exc:  # noqa: BLE001 - см. модульный докстринг: не падаем, но и не молчим
            self._fail("append", exc, trace=str(payload.get("trace_id", "")))

    # ------------------------------------------------ чтение и сверка

    async def latest_trace(self, *, owner_id: int) -> str | None:
        """Последний ход владельца — чтобы «объясни» работало без копирования UUID.

        Отдельного «списка ходов» нет: последний ход — это MAX(seq) по строкам журнала, поэтому
        спросить можно всегда, даже если до этого хода был один вызов модели.
        """
        sql = (
            "SELECT trace_id::text AS trace_id FROM governance.decision_records "
            "WHERE owner_id = :owner_id ORDER BY seq DESC LIMIT 1"
        )
        try:
            async with self._session() as s:
                row = (await s.execute(text(sql), {"owner_id": int(owner_id)})).mappings().first()
                return str(row["trace_id"]) if row else None
        except Exception as exc:  # noqa: BLE001 - объяснение не имеет права ронять ход
            self._fail("latest_trace", exc)
            return None

    async def matching_traces(self, ref: str, *, limit: int = 3) -> list[str]:
        """Найти ход по полному UUID или его началу — возвращаем все совпадения, не угадывая.

        Владельцу печатается короткие 8 символов (``trace=1a2b3c4d``), и просить полный UUID было бы
        «разберись сам». Но молча взять первый из нескольких совпавших хуже отказа: объяснение
        окажется про чужой ход. Поэтому список, а не `str | None`.
        """
        value = (ref or "").strip().lower()
        if not value:
            return []
        if _is_uuid(value):
            return [value]
        if len(value) < 6:
            return []
        sql = (
            "SELECT DISTINCT trace_id::text AS trace_id FROM governance.decision_records "
            "WHERE trace_id::text LIKE :prefix || '%' ORDER BY trace_id LIMIT :limit"
        )
        try:
            async with self._session() as s:
                rows = (
                    (await s.execute(text(sql), {"prefix": value, "limit": int(limit)}))
                    .mappings()
                    .all()
                )
                return [str(row["trace_id"]) for row in rows]
        except Exception as exc:  # noqa: BLE001 - поиск хода не имеет права ронять ответ
            self._fail("matching_traces", exc)
            return []

    async def record_input(self, record: Mapping[str, Any]) -> Any:
        """Распаковать вход записи. ``None`` — содержимое утрачено или это не JSON.

        Чтение блобов остаётся здесь: ``platform.blobs`` — деталь хранения, а replay обязан работать
        с журналом как с интерфейсом, иначе миграция содержимого в объектранилище (MinIO)
        размажется по всему коду.
        """
        return await self.blobs.json(record.get("input_sha"))

    async def records_for_trace(self, trace_id: str) -> list[dict[str, Any]]:
        """Записи хода с раскрытым содержимым — тем, чем объясняют «почему ты так ответил»."""
        if not _is_uuid(trace_id):
            return []
        async with self._session() as s:
            rows = (await s.execute(text(_SELECT_TRACE).bindparams(trace_id=trace_id))).mappings()
            out: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["input"] = await self.blobs.text(item.get("input_sha"))
                item["output"] = await self.blobs.text(item.get("output_sha"))
                out.append(item)
            return out

    async def verify(
        self, *, since: str | None = None, until: str | None = None, limit: int = 5000
    ) -> ChainReport:
        """Пройти цепочку и сказать, цела ли она. Проверка — пересчёт, а не «доверяй записи»."""
        params: dict[str, Any] = {
            "limit": int(limit),
            "since": since or None,
            "until": until or None,
        }
        try:
            async with self._session() as s:
                rows = (await s.execute(text(_SELECT_WINDOW).bindparams(**params))).mappings()
                records = [dict(row) for row in rows]
        except Exception as exc:  # noqa: BLE001 - сломанная сверка не должна выглядеть как «всё цело»
            self._fail("verify", exc)
            return ChainReport(ok=False, problems=[f"не удалось прочитать журнал: {exc}"[:300]])
        report = check_chain(records)
        report.missing_blobs = sum(
            1 for row in records if row.get("input_gone") or row.get("output_gone")
        )
        report.truncated = sum(1 for row in records if row.get("truncated"))
        return report

    async def anchor(self, day: str | None = None) -> AnchorReport:
        """Мерклов корень записей дня в ``governance.anchors``.

        Якорь — то, что можно вынести вовне (внешний штамп, письмо себе, скриншот), не вынося
        содержимое: правка любой записи дня меняет корень.
        """
        target = day or datetime.now(UTC).date().isoformat()
        try:
            async with self._session() as s:
                rows = (
                    await s.execute(
                        text(
                            "SELECT hash FROM governance.decision_records "
                            "WHERE created_at::date = CAST(:day AS date) ORDER BY seq"
                        ).bindparams(day=target),
                    )
                ).all()
                hashes = [bytes(row[0]) for row in rows]
                root = merkle_root(hashes)
                bounds = (
                    await s.execute(
                        text(
                            "SELECT coalesce(min(seq), 0), coalesce(max(seq), 0), count(*) "
                            "FROM governance.decision_records "
                            "WHERE created_at::date = CAST(:day AS date)"
                        ).bindparams(day=target),
                    )
                ).one()
                first_seq, last_seq, count = int(bounds[0]), int(bounds[1]), int(bounds[2])
                note = (
                    "" if count == len(hashes) else f"счёт не совпал: {count} против {len(hashes)}"
                )
                await s.execute(
                    text(
                        """
                        INSERT INTO governance.anchors
                            (day, first_seq, last_seq, records, merkle_root, method, note)
                        VALUES (CAST(:day AS date), :first, :last, :records, :root, :method, :note)
                        ON CONFLICT (day) DO UPDATE
                        SET first_seq = EXCLUDED.first_seq, last_seq = EXCLUDED.last_seq,
                            records = EXCLUDED.records, merkle_root = EXCLUDED.merkle_root,
                            method = EXCLUDED.method, note = EXCLUDED.note, anchored_at = now()
                        """
                    ).bindparams(
                        day=target,
                        first=first_seq,
                        last=last_seq,
                        records=count,
                        root=root,
                        method="sha256-pairwise",
                        note=note or None,
                    ),
                )
                await s.commit()
        except Exception as exc:  # noqa: BLE001
            self._fail("anchor", exc)
            return AnchorReport(ok=False, day=target, note=f"не записали якорь: {exc}"[:300])
        return AnchorReport(
            ok=True,
            day=target,
            records=count,
            merkle_root=root.hex(),
            first_seq=first_seq,
            last_seq=last_seq,
            note="локальный корень; внешний штамп (OpenTimestamps) не ставился",
        )

    async def stats(self) -> dict[str, Any]:
        try:
            async with self._session() as s:
                row = (
                    await s.execute(
                        text(
                            "SELECT (SELECT count(*) FROM governance.decision_records) AS records,"
                            " (SELECT max(seq) FROM governance.decision_records) AS last_seq,"
                            " (SELECT count(*) FROM platform.blobs) AS blobs,"
                            " (SELECT coalesce(sum(size_bytes), 0) FROM platform.blobs) AS bytes,"
                            " (SELECT count(*) FROM governance.anchors) AS anchors,"
                            " (SELECT count(*) FROM governance.decision_records"
                            "   WHERE created_at::date = CURRENT_DATE) AS today"
                        )
                    )
                ).mappings()
                data = dict(row.one())
        except Exception as exc:  # noqa: BLE001 - stats не должен ломать /status
            self._fail("stats", exc)
            return {"enabled": True, "error": repr(exc)[:200], "failures": self.failures}
        return {
            "enabled": True,
            "records": int(data.get("records") or 0),
            "records_today": int(data.get("today") or 0),
            "last_seq": data.get("last_seq"),
            "blobs": int(data.get("blobs") or 0),
            "blob_bytes": int(data.get("bytes") or 0),
            "anchors": int(data.get("anchors") or 0),
            "failures": self.failures,
        }

    # ------------------------------------------------ служебное

    def _session(self) -> Any:
        """Своя сессия на запись; в тестах — подставная фабрика (тот же порт, что у репо)."""
        return self._sm() if self._sm is not None else session()

    def _fail(self, where: str, exc: BaseException, *, trace: str = "") -> None:
        self.failures += 1
        log.warning("repro.failed", where=where, err=repr(exc)[:300], trace_id=trace)


class BlobStore:
    """Контент-адрес: содержимое по его же хэшу, один раз на всех.

    Inline-лимит — не придирка: строка запроса к модели вместе с историей легко переваливает за
    мегабайт, а журнал не имеет права разрастаться до размеров, при которых ``verify`` идёт минуты.
    Усечение помечается в самой записи: «обрезали и не сказали» — это ровно то, из-за чего
    воспроизводимость превращается в самоубеждение.
    """

    def __init__(
        self, session_factory: SessionFactory | None = None, *, max_bytes: int = 1_048_576
    ) -> None:
        self._sm = session_factory
        self.max_bytes = max(1024, int(max_bytes))

    async def put(self, value: Any, *, media_type: str = _JSON) -> BlobRef:
        data = _encode(value)
        if media_type == _JSON:
            # нормализуем только то, что является JSON'ом: хэш обязан сходиться с любым
            # порядком ключей, а redact_secrets теоретически может испортить синтаксис
            try:
                data = canonical_bytes(orjson.loads(data))
            except (orjson.JSONDecodeError, ValueError):
                pass
        size = len(data)
        truncated = size > self.max_bytes
        stored = data[: self.max_bytes] if truncated else data
        sha = sha256_bytes(stored)
        sm = self._sm or session
        async with sm() as s:
            await s.execute(
                text(
                    """
                    INSERT INTO platform.blobs (sha256, size_bytes, media_type, content)
                    VALUES (:sha, :size, :media, :content)
                    ON CONFLICT (sha256) DO NOTHING
                    """
                ).bindparams(sha=sha, size=size, media=media_type, content=stored),
            )
            await s.commit()
        return BlobRef(sha256=sha, size=size, truncated=truncated)

    async def get(self, sha: Any) -> bytes | None:
        raw = _sha_bytes(sha)
        if raw is None:
            return None
        sm = self._sm or session
        async with sm() as s:
            found = await s.scalar(
                text("SELECT content FROM platform.blobs WHERE sha256 = :sha").bindparams(sha=raw),
            )
        return bytes(found) if found is not None else None

    async def text(self, sha: Any) -> str | None:
        data = await self.get(sha)
        return None if data is None else data.decode("utf-8", "replace")

    async def json(self, sha: Any) -> Any:
        data = await self.get(sha)
        if data is None:
            return None
        try:
            return orjson.loads(data)
        except orjson.JSONDecodeError:
            return None


def check_chain(records: Sequence[Mapping[str, Any]]) -> ChainReport:
    """Чистая проверка: без БД и без времени. Именно её и проверяют тесты.

    Различаем три беды, потому что лечатся они по-разному: расхождение хэша (содержимое правили),
    пропуск ``seq`` (строку удалили) и «начало вне окна» (мы просто смотрели не с начала журнала) —
    последнее не поломка, и назвать её поломкой означало бы паниковать на каждом ``--since``.
    """
    problems: list[str] = []
    gaps = 0
    prev: bytes | None = None
    first_seq: int | None = None
    last_seq: int | None = None
    for row in records:
        seq = int(row["seq"])
        actual_prev = bytes(row.get("prev_hash") or bytes(_HASH_LEN))
        if prev is None:
            first_seq = seq
            if actual_prev != bytes(_HASH_LEN):
                problems.append(
                    f"seq {seq}: ссылается на предыдущую запись, которой нет в выборке — "
                    "смотрите цепочку с начала журнала"
                )
        else:
            if actual_prev != prev:
                problems.append(f"seq {seq}: prev_hash ссылается не на предыдущую запись цепочки")
            if seq != (last_seq or 0) + 1:
                gaps += seq - (last_seq or 0) - 1
        recomputed = link_hash(actual_prev, row)
        if bytes(row["hash"]) != recomputed:
            problems.append(f"seq {seq}: хэш не совпадает с содержимым — историю правили")
        prev = bytes(row["hash"])
        last_seq = seq
    return ChainReport(
        ok=not problems and not gaps,
        checked=len(records),
        first_seq=first_seq,
        last_seq=last_seq,
        problems=problems,
        gaps=gaps,
    )


_INSERT_RECORD = """
INSERT INTO governance.decision_records
    (id, trace_id, turn_no, kind, owner_id, prompt_ids, tools_schema_sha, model, params,
     input_sha, output_sha, policy, cost_usd, latency_ms, truncated, note, prev_hash, hash)
VALUES (CAST(:id AS uuid), CAST(:trace_id AS uuid), :turn_no, :kind, :owner_id,
        CAST(:prompt_ids AS jsonb), :tools_schema_sha, :model, CAST(:params AS jsonb),
        :input_sha, :output_sha, CAST(:policy AS jsonb), CAST(:cost_usd AS numeric), :latency_ms,
        :truncated, :note, :prev_hash, :hash)
"""

#: Поля записи, читаемые приложением. `seq` и `created_at` в хэш не входят (см. `record_payload`),
#: но без них невозможны ни сверка порядка, ни окно выборки.
#: Оба запроса статические, и список полей вписан в каждый явно. Сознательное дублирование:
#: SQL, склеенный из переменных, линтер правильно считает подозрительным, а `noqa` на
#: многострочное выражение не поставишь. Шесть строк, прочитанные twice, дешевле, чем
#: объяснение, почему в журнале решений запрос собирается конкатенацией.
#:
#: Присутствие блобов проверяем тем же запросом: «цепочка цела, а содержимое утрачено» — два
#: разных диагноза, и владелец обязан увидеть оба, а не «всё хорошо».
#: `CAST(:since AS timestamptz) IS NULL` читается как «нижней границы нет»: asyncpg иначе не
#: выводит тип параметра из голо `:since IS NULL` и падает на сверке без окна.
_SELECT_WINDOW = """
SELECT
    dr.id::text AS id, dr.trace_id::text AS trace_id, dr.turn_no, dr.seq, dr.kind,
    dr.owner_id, dr.prompt_ids, dr.tools_schema_sha, dr.model, dr.params, dr.input_sha,
    dr.output_sha, dr.policy, dr.cost_usd, dr.latency_ms, dr.truncated, dr.note,
    dr.prev_hash, dr.hash, dr.created_at,
    (dr.input_sha IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM platform.blobs b WHERE b.sha256 = dr.input_sha)) AS input_gone,
       (dr.output_sha IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM platform.blobs b WHERE b.sha256 = dr.output_sha)) AS output_gone
FROM governance.decision_records dr
WHERE (CAST(:since AS timestamptz) IS NULL
       OR dr.created_at >= CAST(:since AS timestamptz))
  AND (CAST(:until AS timestamptz) IS NULL
       OR dr.created_at <= CAST(:until AS timestamptz))
ORDER BY dr.seq
LIMIT :limit
"""

_SELECT_TRACE = """
SELECT
    dr.id::text AS id, dr.trace_id::text AS trace_id, dr.turn_no, dr.seq, dr.kind,
    dr.owner_id, dr.prompt_ids, dr.tools_schema_sha, dr.model, dr.params, dr.input_sha,
    dr.output_sha, dr.policy, dr.cost_usd, dr.latency_ms, dr.truncated, dr.note,
    dr.prev_hash, dr.hash, dr.created_at
FROM governance.decision_records dr
WHERE dr.trace_id = CAST(:trace_id AS uuid)
ORDER BY dr.seq
"""


def _insert_params(payload: Mapping[str, Any], digest: bytes) -> dict[str, Any]:
    policy = payload.get("policy")
    return {
        "id": payload["id"],
        "trace_id": payload["trace_id"],
        "turn_no": int(payload["turn_no"]),
        "kind": payload["kind"],
        "owner_id": int(payload["owner_id"]),
        "prompt_ids": orjson.dumps(list(payload.get("prompt_ids") or [])).decode(),
        "tools_schema_sha": _sha_bytes(payload.get("tools_schema_sha")),
        "model": payload.get("model"),
        "params": orjson.dumps(dict(payload.get("params") or {})).decode(),
        "input_sha": _sha_bytes(payload.get("input_sha")),
        "output_sha": _sha_bytes(payload.get("output_sha")),
        "policy": None if policy is None else orjson.dumps(dict(policy)).decode(),
        "cost_usd": _money(payload.get("cost_usd")),
        "latency_ms": int(payload.get("latency_ms") or 0),
        "truncated": bool(payload.get("truncated")),
        "note": payload.get("note"),
        "prev_hash": bytes(payload["prev_hash"]),
        "hash": digest,
    }


def _encode(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return orjson.dumps(value, default=str)


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True
