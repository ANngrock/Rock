"""Экспорт журнала решений в Langfuse поверх OTLP/HTTP.

Модуль делает ровно одну вещь: превращает строки `governance.decision_records` в OTLP-спан и
отправляет их туда, где их можно посмотреть. Он **не** пишет в журнал и не читает историю чата —
источник истины остаётся база, Langfuse это витрина. Отсюда три правила:

* детерминированные идентификаторы (trace/span выводятся из журнала, а не из `uuid4`): повторный
  экспорт того же окна означает «перезаписать», а не «продублировать», и потому не нужны ни таблица
  прогресса, ни «что я уже отправлял»;
* отказ витрины не имеет права выглядеть как отказ приложения: любая ошибка транспорта — это warning
  и ненулевой код возврата команды, а не исключение из пользовательского пути;
* содержимое берётся из тех же blobs, что и при воспроизведении: если запись была урезана при
  сохранении (`truncated`), в спане это помечено, а не «додумано».

Почему OTLP, а не «просто POST /api/public/ingestion»: пакетный ingestion-API объявлен
устаревшим и на облаке Langfuse v4 выключается 16.11.2026; OTLP-эндпоинт — рекомендуемый путь,
и он принимает HTTP/JSON без protobuf и без OTel-SDK в зависимостях.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import orjson
import structlog

from aegis.platform.config import Settings, settings

__all__ = [
    "ExportFailed",
    "ExportReport",
    "ExportUnavailable",
    "LangfuseTarget",
    "OTLP_TRACES_PATH",
    "spans_for_trace",
    "to_otlp",
]

log = structlog.get_logger(__name__)

#: путь OTLP-приёмника у Langfuse (и в облаке, и в self-hosted >= 3.22)
OTLP_TRACES_PATH = "/api/public/otel/v1/traces"
#: без этого заголовка v4 отдаёт до 10 минут задержки между отправкой и появлением в UI
INGESTION_VERSION_HEADER = "x-langfuse-ingestion-version"
#: потолок одного запроса: Langfuse режет тела больше ~3.5-4.5 МБ, мы держимся заметно ниже
MAX_REQUEST_BYTES = 2_000_000
#: вес конверта OTLP (`resourceSpans`/`scopeSpans`): сотни байт, учитываем грубо и с запасом
_ENVELOPE_BYTES = 512
#: сколько символов input/output уезжает наружу; журнал хранит полное содержимое
DEFAULT_MAX_CHARS = 6_000

#: вид записи журнала -> тип наблюдения Langfuse (`langfuse.observation.type`)
KIND_TO_TYPE = {
    "llm_call": "generation",
    "tool_run": "tool",
    "policy": "guardrail",
    "verdict": "evaluator",
    "turn_summary": "span",
}


class ExportUnavailable(RuntimeError):
    """Экспортировать некуда: не включено, нет хоста или ключей. Это настройка, а не авария."""


class ExportFailed(RuntimeError):
    """Витрина не приняла данные: сеть, 4xx/5xx, неожиданный ответ."""


@dataclass(frozen=True, slots=True)
class LangfuseTarget:
    """Куда и под чем postить. Ключи живут только здесь и в заголовке запроса."""

    host: str
    public_key: str
    secret_key: str
    timeout_s: float = 10.0
    max_chars: int = DEFAULT_MAX_CHARS

    @classmethod
    def from_settings(cls, cfg: Settings | None = None) -> LangfuseTarget:
        cfg = cfg or settings()
        if not cfg.langfuse_enabled:
            raise ExportUnavailable(
                "LANGFUSE_ENABLED=false: экспорт выключен, журнал остаётся источником истины"
            )
        host = (cfg.langfuse_host or "").strip().rstrip("/")
        if not host:
            raise ExportUnavailable("LANGFUSE_HOST пуст: укажите облако или self-hosted инстанс")
        if not (cfg.langfuse_public_key and cfg.langfuse_secret_key):
            raise ExportUnavailable(
                "нужны LANGFUSE_PUBLIC_KEY и LANGFUSE_SECRET_KEY (пара ключей проекта в Langfuse)"
            )
        return cls(
            host=host,
            public_key=cfg.langfuse_public_key,
            secret_key=cfg.langfuse_secret_key,
            timeout_s=float(cfg.langfuse_timeout_s),
            max_chars=int(cfg.langfuse_max_chars),
        )

    @property
    def url(self) -> str:
        return f"{self.host}{OTLP_TRACES_PATH}"

    @property
    def headers(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.public_key}:{self.secret_key}".encode()).decode()
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            INGESTION_VERSION_HEADER: "4",
        }

    def describe(self) -> str:
        """Строка для лога и `doctor`: ключи сюда попадать не должны, никогда."""
        host = self.host.split("://", 1)[-1]
        return f"{host}{OTLP_TRACES_PATH} · pk {self.public_key[:6]}…"


def _bytes_to_hex(data: bytes) -> str:
    return data.hex()


def _trace_hex(trace_id: str) -> str:
    """32 hex-символа из UUID (или из хэша, если идентификатор не UUID)."""
    raw = (trace_id or "").replace("-", "").lower()
    if len(raw) == 32 and all(char in "0123456789abcdef" for char in raw):
        return raw
    return hashlib.sha256(trace_id.encode()).hexdigest()[:32]


def _span_hex(seed: str) -> str:
    """16 hex-символа, стабильных для одного и того же seed'а."""
    digest = hashlib.sha256(seed.encode()).hexdigest()[:16]
    return digest if int(digest, 16) else "0000000000000001"


def _nano(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return str(int(moment.timestamp() * 1_000_000_000))


def _attr(key: str, value: Any) -> dict[str, Any]:
    """Атрибут OTLP: только те типы, которые действительно есть в спецификации JSON."""
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    if isinstance(value, (list, tuple)):
        values = [_attr("", item)["value"] for item in value]
        return {"key": key, "value": {"arrayValue": {"values": values}}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _content_attr(key: str, text: str, max_chars: int) -> dict[str, Any]:
    """Атрибут с содержимым: срез не имеет права ломать JSON, который витрина будет парсить.

    Блобы хранятся каноническим ASCII-JSON (ADR-0008: хэш обязан сходиться с любым порядком
    ключей), поэтому в середине среза спокойно может торчать `\\u04`. Для строки это безобидно,
    для «это JSON» — сломанный просмотр, и мы превращаем срез в объект с превью вместо того,
    чтобы выдавать обрезанный документ за документ.
    """
    clipped = _clip(text, max_chars)
    if clipped != text and text[:1] in "{[":
        return _attr(
            key,
            orjson.dumps({"aegis_truncated_at_chars": max_chars, "preview": clipped}).decode(),
        )
    return _attr(key, clipped)


def _ru(count: int, one: str, few: str, many: str) -> str:
    """Согласование числительного по-русски: «4 спана», а не «4 спанов»."""
    mod10, mod100 = count % 10, count % 100
    if mod10 == 1 and mod100 != 11:
        return one
    if 2 <= mod10 <= 4 and not 12 <= mod100 <= 14:
        return few
    return many


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8", "replace")
        except UnicodeError:  # pragma: no cover - decode(replace) не бросает
            return ""
    if isinstance(value, str):
        return value
    return orjson.dumps(value, default=str).decode()


@dataclass(slots=True)
class ExportReport:
    """Что уехало и что помешало. `summary()` — единственная форма, которую видит человек."""

    traces: int = 0
    spans: int = 0
    batches: int = 0
    dry_run: bool = False
    stopped: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.stopped

    def summary(self) -> str:
        spans = f"{self.spans} {_ru(self.spans, 'спан', 'спана', 'спанов')}"
        traces = f"{self.traces} {_ru(self.traces, 'трейсе', 'трейсах', 'трейсах')}"
        batches = f"{self.batches} {_ru(self.batches, 'запрос', 'запроса', 'запросов')}"
        base = (
            f"{spans} в {traces} · отправлено {batches}"
            if not self.dry_run
            else f"ушли бы {spans} в {traces} · {batches}"
        )
        if self.stopped:
            base += f" · остановлено: {self.stopped}"
        if self.errors:
            base += f" · ошибок: {len(self.errors)}"
        return base


def spans_for_trace(
    records: Sequence[Mapping[str, Any]], *, max_chars: int = DEFAULT_MAX_CHARS
) -> list[dict[str, Any]]:  # noqa: D401 - список спанов, первый из которых корень
    """Записи одного хода -> корневой спан + по спану на запись.

    Корень нужен не для красоты: Langfuse строит трейс от корневого спана, и именно на корень
    вешаются trace-level атрибуты (v4 берёт input/output из корневого наблюдения).
    """
    if not records:
        return []
    trace_id = str(records[0].get("trace_id") or "")
    trace_hex = _trace_hex(trace_id)
    owner_id = int(records[0].get("owner_id") or 0)
    turn_no = int(records[0].get("turn_no") or 0)
    first = records[0].get("created_at")
    root_start = first if isinstance(first, datetime) else datetime.now(UTC)
    root_span: dict[str, Any] = {
        "traceId": trace_hex,
        "spanId": _span_hex(f"root:{trace_id}"),
        "name": f"turn #{turn_no}",
        "kind": 1,  # SPAN_KIND_INTERNAL
        "startTimeUnixNano": _nano(root_start),
        "endTimeUnixNano": _nano(_last_moment(records, root_start)),
        "attributes": [
            _attr("langfuse.trace.name", f"aegis turn #{turn_no}"),
            _attr("langfuse.user.id", str(owner_id)),
            _attr("langfuse.session.id", f"owner-{owner_id}"),
            _attr("langfuse.trace.metadata.aegis.trace_id", trace_id),
            _attr("langfuse.observation.type", "span"),
        ],
        "status": {"code": 1},
    }
    spans: list[dict[str, Any]] = [root_span]
    for index, record in enumerate(records):
        spans.append(
            _span_for(
                record,
                trace_hex=trace_hex,
                parent=root_span["spanId"],
                index=index,
                max_chars=max_chars,
            )
        )
    return spans


def _last_moment(records: Sequence[Mapping[str, Any]], fallback: datetime) -> datetime:
    for record in reversed(records):
        moment = record.get("created_at")
        if isinstance(moment, datetime):
            return moment
    return fallback


def _params_of(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """`params` журнала — jsonb: либо словарь, либо пусто;None сюда попадать не должно."""
    raw = record.get("params")
    return raw if isinstance(raw, Mapping) else {}


def _span_for(
    record: Mapping[str, Any], *, trace_hex: str, parent: str, index: int, max_chars: int
) -> dict[str, Any]:
    kind = str(record.get("kind") or "span")
    record_id = str(record.get("id") or f"{trace_hex}:{index}")
    params: Mapping[str, Any] = _params_of(record)
    moment = record.get("created_at")
    start = moment if isinstance(moment, datetime) else datetime.now(UTC)
    latency = int(record.get("latency_ms") or 0)
    end = start + timedelta(milliseconds=latency) if latency > 0 else start
    model = str(record.get("model") or "")
    note = str(record.get("note") or "")
    name = f"{kind}:{model}" if kind == "llm_call" and model else kind
    attributes: list[dict[str, Any]] = [
        _attr("langfuse.observation.type", KIND_TO_TYPE.get(kind, "span")),
        _attr("langfuse.observation.metadata.aegis.seq", int(record.get("seq") or 0)),
        _attr("langfuse.observation.metadata.aegis.record_id", record_id),
        _attr("langfuse.observation.metadata.aegis.hash", _bytes_to_hex(record.get("hash") or b"")),
        _attr(
            "langfuse.observation.metadata.aegis.truncated",
            bool(record.get("truncated")),
        ),
    ]
    input_text = _as_text(record.get("input_text"))
    output_text = _as_text(record.get("output_text"))
    if input_text:
        attributes.append(_content_attr("langfuse.observation.input", input_text, max_chars))
    if output_text:
        attributes.append(_content_attr("langfuse.observation.output", output_text, max_chars))
    if kind == "llm_call":
        attributes.extend(_generation_attrs(record, params, model))
    if kind == "tool_run":
        attributes.append(
            _attr("langfuse.observation.metadata.aegis.tool", str(params.get("tool", "")))
        )
        attributes.append(
            _attr("langfuse.observation.metadata.aegis.trust", str(params.get("trust", "")))
        )
    if kind in ("policy", "verdict") and record.get("policy") is not None:
        attributes.append(
            _attr(
                "langfuse.observation.metadata.aegis.decision",
                _as_text(record.get("policy"))[:400],
            )
        )
    if note:
        attributes.append(_attr("langfuse.observation.status_message", _clip(note, 400)))
    failed = params.get("ok") is False or (kind in ("policy", "verdict") and _denied(record))
    level = "ERROR" if failed else "DEFAULT"
    if level == "ERROR":
        attributes.append(_attr("langfuse.observation.level", "ERROR"))
    return {
        "traceId": trace_hex,
        "spanId": _span_hex(f"{record_id}:{index}"),
        "parentSpanId": parent,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": _nano(start),
        "endTimeUnixNano": _nano(end),
        "attributes": attributes,
        "status": {"code": 2 if level == "ERROR" else 1},
    }


def _denied(record: Mapping[str, Any]) -> bool:
    decision = record.get("policy")
    if isinstance(decision, Mapping):
        return str(decision.get("decision", "")).lower().startswith(("deny", "block"))
    return "deny" in str(decision or "").lower()


def _generation_attrs(
    record: Mapping[str, Any], params: Mapping[str, Any], model: str
) -> list[dict[str, Any]]:
    """Токены, стоимость и ссылки на промпты — в поля, которые Langfuse понимает как usage/cost."""
    out: list[dict[str, Any]] = []
    if model:
        out.append(_attr("langfuse.observation.model.name", model))
    prompt_tokens = int(params.get("prompt_tokens") or 0)
    completion_tokens = int(params.get("completion_tokens") or 0)
    if prompt_tokens or completion_tokens:
        usage = {
            "input": prompt_tokens,
            "output": completion_tokens,
            "total": prompt_tokens + completion_tokens,
        }
        out.append(_attr("langfuse.observation.usage_details", orjson.dumps(usage).decode()))
    cost = float(record.get("cost_usd") or 0.0)
    if cost:
        out.append(
            _attr("langfuse.observation.cost_details", orjson.dumps({"total": cost}).decode())
        )
    # промпты у нас версионируются в репозитории (ADR-0008), а не в Langfuse: поле
    # `langfuse.observation.prompt.*` требует ссылки на *их* реестр и целую версию, поэтому
    # ссылки (id + версия + sha256 текста) уезжают фильтруемым metadata — смысл тот же,
    # зато без выдавания чужого имени за управляемый промпт
    refs = record.get("prompt_ids")
    if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)) and refs:
        out.append(
            _attr(
                "langfuse.observation.metadata.aegis.prompt_ids",
                orjson.dumps([dict(ref) for ref in refs if isinstance(ref, Mapping)]).decode()[
                    :2_000
                ],
            )
        )
    if params.get("role"):
        out.append(_attr("langfuse.observation.metadata.aegis.role", str(params["role"])))
    if params.get("attempt"):
        out.append(_attr("langfuse.observation.metadata.aegis.attempt", int(params["attempt"])))
    return out


def to_otlp(
    traces: Sequence[Sequence[dict[str, Any]]], *, service_name: str = "aegis"
) -> dict[str, Any]:
    """Списки спанов по трейсам -> тело OTLP/HTTP. Один вызов = один запрос.

    Спанам не нужно группироваться по трейсам: в OTLP трейс определяется полем `traceId` у самого
    спана, поэтому один запрос спокойно несёт несколько ходов.
    """
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attr("service.name", service_name)]},
                "scopeSpans": [
                    {
                        "scope": {"name": "aegis.journal", "version": "1"},
                        "spans": [dict(span) for trace in traces for span in trace],
                    }
                ],
            }
        ]
    }


def _batches(traces: Sequence[Sequence[dict[str, Any]]]) -> list[list[list[dict[str, Any]]]]:
    """Группировка трейсов в запросы по размеру тела, а не по количеству трейсов.

    Размер решает всё: 400 маленьких трейсов и один трейс с мегабайтным промптом — это разные
    риски, и второй обязан уехать отдельным запросом, а не упереться в лимит тела.
    """
    out: list[list[list[dict[str, Any]]]] = []
    current: list[list[dict[str, Any]]] = []
    size = _ENVELOPE_BYTES
    for trace in traces:
        # размер считаем один раз на трейс: пересобирать всё накопленное на каждой итерации —
        # квадратичная работа на окне в несколько сотен ходов, ровно там, где экспорт и включают
        payload = list(trace)
        weight = len(orjson.dumps(payload))
        if current and size + weight > MAX_REQUEST_BYTES:
            out.append(current)
            current, size = [payload], _ENVELOPE_BYTES + weight
        else:
            current.append(payload)
            size += weight
    if current:
        out.append(current)
    return out


SELECT_WINDOW = """
SELECT
    dr.id::text AS id, dr.trace_id::text AS trace_id, dr.turn_no, dr.seq, dr.kind,
    dr.owner_id, dr.prompt_ids, dr.model, dr.params, dr.policy, dr.cost_usd, dr.latency_ms,
    dr.truncated, dr.note, dr.hash, dr.created_at,
    ib.content AS input_content, ob.content AS output_content
FROM governance.decision_records dr
LEFT JOIN platform.blobs ib ON ib.sha256 = dr.input_sha
LEFT JOIN platform.blobs ob ON ob.sha256 = dr.output_sha
WHERE dr.created_at >= CAST(:since AS timestamptz)
  AND (CAST(:until AS timestamptz) IS NULL OR dr.created_at <= CAST(:until AS timestamptz))
  AND (CAST(:only_trace AS uuid) IS NULL OR dr.trace_id = CAST(:only_trace AS uuid))
ORDER BY dr.trace_id, dr.seq
LIMIT CAST(:limit AS int)
"""


async def collect(
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 500,
    trace_id: str = "",
    session_factory: Any = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[list[dict[str, Any]]]:
    """Прочитать окно журнала и собрать спаны по трейсам. Ни одного сетевого вызова.

    `since` по умолчанию — сутки назад: перечитать окно безопасно именно благодаря
    детерминированным идентификаторам, поэтому «что же там накопилось» не требует состояния.
    """
    from aegis.platform.db import session as _session

    factory = session_factory or _session
    since = since or datetime.now(UTC) - timedelta(hours=24)
    params = {
        "since": since,
        "until": until,
        "limit": int(limit),
        "only_trace": trace_id or None,
    }
    async with factory() as s:
        rows = (await s.execute(_sql().bindparams(**params))).mappings()
        records = [dict(row) for row in rows]
    by_trace: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        # здесь не режем: одно место среза (`_content_attr`), иначе «обрезано дважды» выглядело бы
        # как содержимое, а срезы разной длины расходились бы с тем, что видно в журнале
        row["input_text"] = _as_text(row.pop("input_content", None))
        row["output_text"] = _as_text(row.pop("output_content", None))
        by_trace.setdefault(str(row.get("trace_id") or ""), []).append(row)
    return [spans_for_trace(items, max_chars=max_chars) for items in by_trace.values()]


def _sql() -> Any:
    from sqlalchemy import text

    return text(SELECT_WINDOW)


async def send(payload: dict[str, Any], target: LangfuseTarget, *, client: Any = None) -> None:
    """Один POST. Чужой `client` нужен тестам и тому, чтобы не плодить соединения в цикле."""
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=target.timeout_s)
    try:
        response = await client.post(
            target.url, headers=target.headers, content=orjson.dumps(payload)
        )
    except httpx.HTTPError as exc:
        raise ExportFailed(f"Langfuse не отвечает: {type(exc).__name__}: {str(exc)[:200]}") from exc
    finally:
        if own_client:
            await client.aclose()
    if response.status_code not in (200, 201, 202, 204, 206, 207):
        raise ExportFailed(f"Langfuse ответил {response.status_code}: {response.text[:200]}")


async def export_window(
    *,
    target: LangfuseTarget | None = None,
    cfg: Settings | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
    trace_id: str = "",
    dry_run: bool = False,
    client: Any = None,
    session_factory: Any = None,
) -> ExportReport:
    """Собрать окно журнала и отправить пачками. Остановка на первом отказе — осознанная."""
    cfg = cfg or settings()
    if target is None:
        target = LangfuseTarget.from_settings(cfg)
    limit = int(limit or cfg.langfuse_limit)
    since = since or datetime.now(UTC) - timedelta(hours=int(cfg.langfuse_window_hours))
    traces = await collect(
        since=since,
        until=until,
        limit=limit,
        trace_id=trace_id,
        session_factory=session_factory,
        max_chars=target.max_chars,
    )
    batches = _batches(traces)
    report = ExportReport(
        traces=len(traces),
        spans=sum(len(trace) for trace in traces),
        dry_run=dry_run,
    )
    if dry_run:
        report.batches = len(batches)
        return report
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=target.timeout_s)
    try:
        for batch in batches:
            try:
                await send(to_otlp(batch), target, client=client)
            except ExportFailed as exc:
                # дальше не идём: половинчатая витрина хуже полной, а повтор того же окна дёшев
                report.stopped = str(exc)[:300]
                report.errors.append(str(exc)[:300])
                log.warning("langfuse.export_failed", err=str(exc)[:300], batch=report.batches)
                return report
            report.batches += 1
    finally:
        if owns_client:
            await client.aclose()
    return report
