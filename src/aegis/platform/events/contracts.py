"""Контракт событий (F4): envelope с версией, реестр схем, DLQ и replay.

До сих пор «схема события» = «что лежит в таблице»: подписчик узнавал об изменении ``payload``
только когда падал. Этот слой превращает формат в контракт:

* **envelope** (общая часть) и **payload-схема на каждый event_type** в реестре; ``schema_version``
  обязателен и входит в конверт;
* **обратная совместимость проверяется CI**: ``schema/released/events-v1.json`` — замороженный
  снимок реестра; сравнение с живым реестром находит breaking-изменения (новый обязательный
  параметр, удалённое поле, сузившийся enum), и любое из них требует завести ``version+1``, а не
  «поправить v1 незаметно»;
* **DLQ**: событие, отравляющее очередь, не блокирует остальных —relay помечает строку
  ``abandoned_at`` и пишет копию в ``platform.event_dlq`` с причиной и полным телом;
* **replay** ``aegis events replay --from-seq N`` идемпотентен: он сбрасывает отметки доставки,
  и повторный запуск не создаёт дублей (вещает тот же ``event_id`` — дедупликация на
  потребителе, как и обещает ADR-0013).

Порядок гарантируется **внутри stream_id** (version-нумерация потока), а не глобально: «общий
порядок для всех» — ложное обещание на одной очереди, и мы его не даём.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "DLQ_SUBJECT",
    "ENVELOPE_SCHEMA",
    "EVENT_REGISTRY",
    "ContractError",
    "breaking_changes",
    "build_envelope",
    "consumer_config",
    "current_registry_export",
    "known_event_types",
    "validate_envelope",
]

#: субъект dead-letter-потока: причина отказа и исходное тело; подписчики алертов читают только его
DLQ_SUBJECT = "aegis.dlq.events"

_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_STREAM_RE = r"^[A-Za-z0-9_:.@-]{1,160}$"

#: общий конверт. Всё, что relay читает из строки outbox, — здесь; payload'и описывает реестр
ENVELOPE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:aegis:envelope:1",
    "type": "object",
    "required": [
        "event_id",
        "event_type",
        "schema_version",
        "occurred_at",
        "stream_type",
        "stream_id",
        "version",
        "payload",
    ],
    "properties": {
        "event_id": {"type": "string", "pattern": _UUID_RE, "description": "ключ дедупликации"},
        "event_type": {"type": "string", "pattern": r"^[a-z][a-z0-9_.]{2,80}$"},
        "schema_version": {"type": "integer", "minimum": 1},
        "occurred_at": {"type": "string", "minLength": 10},
        "causation_id": {"type": ["string", "null"]},
        "stream_type": {"type": "string", "pattern": _STREAM_RE},
        "stream_id": {"type": "string", "pattern": _STREAM_RE},
        "version": {
            "type": "integer",
            "minimum": 1,
            "description": "номер в потоке (порядок внутри потока)",
        },
        "payload": {"type": "object"},
    },
    "additionalProperties": True,
}


def _event(version: int, schema: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": version, "payload": schema}


#: реестр типов, которые приложение испускает. Новый emit без записи сюда — падение теста
#: ``test_event_registry_covers_code``: «опубликовали без контракта» перестало быть нормальным
#: состоянием мира
EVENT_REGISTRY: dict[str, dict[str, Any]] = {
    "conversation.turn_received": _event(
        1,
        {
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {"type": "string", "maxLength": 4000},
                "attachments": {"type": "integer", "minimum": 0},
                "role": {"type": "string"},
                "thinking": {"type": "boolean"},
                "prompt_version": {"type": "string"},
                "source_trust": {"type": "string", "enum": ["owner", "untrusted", "system"]},
            },
        },
    ),
    "conversation.reset": _event(1, {"type": "object", "properties": {}}),
    "intent.answered": _event(
        1,
        {
            "type": "object",
            "required": ["intent"],
            "properties": {"intent": {"type": "string", "minLength": 1}},
        },
    ),
    "policy.decision": _event(
        1,
        {
            "type": "object",
            "required": ["tool", "decision"],
            "properties": {
                "tool": {"type": "string"},
                "decision": {"type": "string", "enum": ["allow", "confirm", "deny"]},
                "reason": {"type": "string"},
                "rule": {"type": ["string", "null"]},
            },
        },
    ),
    "tool.executed": _event(
        1,
        {
            "type": "object",
            "required": ["tool"],
            "properties": {
                "tool": {"type": "string"},
                "decision": {"type": "string"},
                "trust": {"type": "string"},
                "ok": {"type": "boolean"},
                "result_len": {"type": "integer", "minimum": 0},
                "result": {"type": "string", "maxLength": 4000},
            },
        },
    ),
    "tool.failed": _event(
        1,
        {
            "type": "object",
            "required": ["tool"],
            "properties": {"tool": {"type": "string"}, "err": {"type": "string"}},
        },
    ),
    "quarantine.instructions": _event(
        1,
        {
            "type": "object",
            "required": ["tool"],
            "properties": {
                "tool": {"type": "string"},
                "found": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
            },
        },
    ),
    "action.confirmation_requested": _event(
        1,
        {
            "type": "object",
            "required": ["pending_id"],
            "properties": {
                "pending_id": {"type": "string"},
                "tools": {"type": "array", "items": {"type": "string"}},
            },
        },
    ),
    "action.confirmation_resolved": _event(
        1,
        {
            "type": "object",
            "required": ["tool", "approved"],
            "properties": {
                "tool": {"type": "string"},
                "approved": {"type": "boolean"},
                "trace_id": {"type": "string"},
            },
        },
    ),
    "action.confirmation_unavailable": _event(
        1,
        {
            "type": "object",
            "required": ["tools"],
            "properties": {"tools": {"type": "array", "items": {"type": "string"}}},
        },
    ),
    "answer.verified": _event(
        1,
        {
            "type": "object",
            "required": ["ok"],
            "properties": {
                "ok": {"type": "boolean"},
                "severity": {"type": "string"},
                "mode": {"type": "string"},
                "problems": {"type": "array", "items": {"type": "string"}},
            },
        },
    ),
}


class ContractError(ValueError):
    """Событие не проходит собственный контракт — публиковать его нельзя, надо чинить издателя."""


def build_envelope(
    *,
    event_id: str,
    event_type: str,
    schema_version: int,
    occurred_at: str,
    stream_type: str,
    stream_id: str,
    version: int,
    payload: Mapping[str, Any],
    causation_id: str | None = None,
) -> dict[str, Any]:
    """Собрать конверт. Единая точка сборки обязывает: «envelope» собирают все, а ошибаются —
    наперегонки."""
    return {
        "event_id": event_id,
        "event_type": event_type,
        "schema_version": int(schema_version),
        "occurred_at": occurred_at,
        "causation_id": causation_id,
        "stream_type": stream_type,
        "stream_id": stream_id,
        "version": int(version),
        "payload": dict(payload),
    }


def validate_envelope(
    envelope: Mapping[str, Any], *, registry: Mapping[str, Any] | None = None
) -> list[str]:
    """Ошибки контракта списком (пустой = «валидно»). Не бросаем: relay копит диагностику, а не
    падает."""
    import jsonschema  # noqa: PLC0415 — зависимость проверки, не пути ответа

    errors: list[str] = []
    validator = jsonschema.Draft202012Validator(ENVELOPE_SCHEMA)
    for err in validator.iter_errors(envelope):
        errors.append(f"envelope: {err.message}"[:300])
    event_type = str(envelope.get("event_type", ""))
    known = registry or EVENT_REGISTRY
    spec = known.get(event_type)
    if spec is None:
        errors.append(
            f"нет контракта для event_type {event_type!r}: заведите запись в EVENT_REGISTRY"
        )
        return errors
    got_version = int(envelope.get("schema_version") or 0)
    want_version = int(spec["schema_version"])
    if got_version != want_version:
        errors.append(
            f"{event_type}: издатели на version {got_version} нет — контракт знает только "
            f"v{want_version}; ужесточение/смена формы = новая версия"
        )
        return errors
    payload_validator = jsonschema.Draft202012Validator(spec["payload"])
    for err in payload_validator.iter_errors(envelope.get("payload") or {}):
        path = ".".join(str(p) for p in err.absolute_path) or "payload"
        errors.append(f"{event_type}@v{want_version} {path}: {err.message}"[:300])
    return errors


def known_event_types() -> Sequence[str]:
    return tuple(sorted(EVENT_REGISTRY))


# ------------------------------------------------------------------ совместимость


def _required_keys(schema: Mapping[str, Any]) -> set[str]:
    return {str(k) for k in schema.get("required") or []}


def _properties(schema: Mapping[str, Any]) -> dict[str, Any]:
    props = schema.get("properties") or {}
    return dict(props) if isinstance(props, Mapping) else {}


def _payload_shape(entry: Mapping[str, Any]) -> dict[str, Any]:
    return dict(entry.get("payload") or {})


def breaking_changes(released: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Что в текущем реестре ломает потребителей выпущенной версии. Пусто = совместимо.

    Правила намеренно консервативны и объяснимы:

    * событие пропало из реестра — breaking (подписчик не узнает, куда делся тип);
    * версия контракта снижена — breaking;
    * добавлены обязательные поля, удалены поля, сузился enum, изменился тип, закрыт
      ``additionalProperties`` — breaking: старый producer начинает «не проходить»;
    * расширение необязательных полей и ослабления — совместимы: consumers обязаны игнорировать
      неизвестное (то же обещание, что у protobuf).
    """
    released_events = dict((released or {}).get("events") or {})
    current_events = dict((current or {}).get("events") or {})
    problems: list[str] = []
    for name, old in released_events.items():
        new = current_events.get(name)
        if new is None:
            problems.append(f"{name}: тип удалён из реестра")
            continue
        old_version, new_version = (
            int(old.get("schema_version", 1)),
            int(new.get("schema_version", 1)),
        )
        if new_version < old_version:
            problems.append(f"{name}: schema_version снижена {old_version} → {new_version}")
            continue
        if new_version != old_version:
            # отдельная версия — осознанный break, он легален сам по себе
            continue
        old_shape, new_shape = _payload_shape(old), _payload_shape(new)
        added_required = _required_keys(new_shape) - _required_keys(old_shape)
        if added_required:
            problems.append(
                f"{name}@v{old_version}: добавлены обязательные поля {sorted(added_required)}"
            )
        removed = set(_properties(old_shape)) - set(_properties(new_shape))
        if removed:
            problems.append(f"{name}@v{old_version}: поля исчезли {sorted(removed)}")
        for prop in sorted(set(_properties(old_shape)) & set(_properties(new_shape))):
            old_prop = (
                old_shape["properties"][prop]
                if isinstance(old_shape["properties"], Mapping)
                else {}
            )
            new_prop = (
                new_shape["properties"][prop]
                if isinstance(new_shape["properties"], Mapping)
                else {}
            )
            if not isinstance(old_prop, Mapping) or not isinstance(new_prop, Mapping):
                continue
            if old_prop.get("type") != new_prop.get("type"):
                problems.append(f"{name}@v{old_version}.{prop}: тип изменён")
            old_enum, new_enum = old_prop.get("enum"), new_prop.get("enum")
            if old_enum is not None and new_enum is not None and not set(old_enum) >= set(new_enum):
                problems.append(f"{name}@v{old_version}.{prop}: enum сузился")
            if old_prop.get("maxLength") is not None and new_prop.get("maxLength") is not None:
                if int(new_prop["maxLength"]) < int(old_prop["maxLength"]):
                    problems.append(f"{name}@v{old_version}.{prop}: maxLength ужесточён")
        if (
            old_shape.get("additionalProperties") is not False
            and new_shape.get("additionalProperties") is False
        ):
            problems.append(f"{name}@v{old_version}: включён additionalProperties:false")
    return problems


def current_registry_export() -> dict[str, Any]:
    """Тот же формат, что и снимок в ``schema/released/``: CI сравнивает файлы, а не «память
    людей»."""
    return {
        "envelope": ENVELOPE_SCHEMA,
        "events": {name: dict(spec) for name, spec in sorted(EVENT_REGISTRY.items())},
    }


# ------------------------------------------------------------------ потребители


def consumer_config(
    *,
    stream: str = "aegis_events",
    durable_name: str = "aegis-consumer",
    max_deliver: int = 10,
    ack_wait_s: float = 30.0,
    backoff: Sequence[float] = (1.0, 5.0, 30.0, 120.0),
    filter_subjects: Sequence[str] = ("aegis.*.*.*",),
) -> dict[str, Any]:
    """Параметры JetStream-потребителя. Отданы наружу как данные, чтобы конфиг можно было
    проверять тестом и скармливать ``nats consumer add`` — а не читать в доках «примерно так».

    ``ack_explicit`` — принципиально: ack-all «пока жив процесс» не гарантирует, что обработчик
    закончил; после DLQ-политики максимум доставок обязан быть конечным, иначе poison-событие
    крутится вечно.
    """
    return {
        "stream": stream,
        "durable_name": durable_name,
        "ack_policy": "explicit",
        "ack_wait": ack_wait_s,
        "max_deliver": max_deliver,
        "max_waiting": 1000,
        "filter_subjects": list(filter_subjects),
        "backoff": list(backoff),
        "deliver_policy": "new",
        "dead_letter": {"subject": DLQ_SUBJECT, "after_attempts": max_deliver},
    }
