"""Каноническое представление данных для цепочки воспроизводимости.

Зачем отдельный модуль: хэш записи — единственное, чем журнал может *доказать*, что ему можно
верить. Если сериализация зависит от порядка ключей, локали или `repr` float, цепочка рвётся на
ровном месте, а «неизменяемый журнал» превращается в «мы не знаем, кто поменял байты».

Про стандарт. В плане фигурирует RFC 8785 (JCS); мы повторяем его суть — сортировка ключей,
минимальные разделители, ASCII-экранирование (снимает вопрос о NFC/NFD), запрет NaN. Не реализована
одна часть JCS: сериализация чисел по ECMA-262. Поэтому денежные и прочие точные поля в
журнале — **строки** с фиксированным числом знаков (`"0.000123"`, а не `1.23e-4`). Сознательная
сделка: хэш обязан быть воспроизводимым стандартными средствами Python, а не «стандарт ради
стандарта».
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from pathlib import PurePath
from typing import Any

__all__ = [
    "Unrecordable",
    "canonical_bytes",
    "canonical_sha256",
    "sha256_bytes",
    "sha256_hex",
    "to_canonical_value",
]


def _dumps(value: Any) -> str:
    """Один способ сериализовать — и он же единственный источник истины для хэшей.

    ``sort_keys`` + компактные разделители + ASCII + запрет NaN: одинаковый объект даёт одинаковые
    байты на любой системе. kwargs передаём явно (а не ``**dict``): mypy прав, что такой dict не
    доказывает совместимость с подписью ``json.dumps``, а «канон» — ровно то место, где тайп-чек
    полезнее экономии двух строк.
    """
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    )


class Unrecordable(ValueError):
    """Значение нельзя канонизировать — значит, нельзя и поставить в цепочку.

    Скрипеть и сериализовать «почти то же самое» хуже: цепочка осталась бы целой, а воспроизвести
    вход по записи было бы уже нельзя.
    """


def to_canonical_value(value: Any) -> Any:
    """Привести к JSON-совместимому виду.

    Числа — строками: `0.1 + 0.2` в float это `0.30000000000000004`, и договориться о формате
    строки проще, чем отлаживать расхождение хэшей из-за `repr`.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise Unrecordable(f"нечисловое значение в записи: {value!r}")
        return f"{value:.6f}"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, int):
        if abs(value) >= 2**63:
            raise Unrecordable("целое вне диапазона журнала")
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "hex:" + bytes(value).hex()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return to_canonical_value(value.value)
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, dict):
        return {
            (key if isinstance(key, str) else str(key)): to_canonical_value(item)
            for key, item in value.items()
        }
    asdict_method = getattr(value, "_asdict", None)
    if callable(asdict_method):  # namedtuple: имена полей значимы, поэтому до общей ветки tuple
        return to_canonical_value(asdict_method())
    if isinstance(value, (list, tuple)):
        return [to_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        # множество не упорядочено → порядок в хэше был бы случайным; сортируем по канону элемента
        return sorted(
            (to_canonical_value(item) for item in value),
            key=lambda item: _dumps(item),
        )
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):  # pydantic v2
        return to_canonical_value(model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return to_canonical_value(asdict(value))
    raise Unrecordable(f"не умею канонизировать {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    """Байты канонического JSON: один и тот же объект → одни и те же байты всегда."""
    return _dumps(to_canonical_value(value)).encode("ascii")


def sha256_bytes(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value: Any) -> bytes:
    """Хэш значения (32 байта) — именно это кладётся в `hash`/`prev_hash` записей."""
    return sha256_bytes(canonical_bytes(value))
