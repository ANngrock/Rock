"""Канонизация значений для цепочки воспроизводимости.

Здесь проверяется не «красивый JSON», а единственное свойство, ради которого модуль существует:
одинаковое значение → одинаковые байты, а любое значение, которое мы не умеем описать однозначно,
— отказ, а не правдоподобная догадка.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import PurePosixPath

import pytest
from pydantic import BaseModel

from aegis.platform.canonical import (
    Unrecordable,
    canonical_bytes,
    canonical_sha256,
    sha256_bytes,
    sha256_hex,
    to_canonical_value,
)


def test_key_order_does_not_change_bytes() -> None:
    a = {"b": 1, "a": {"y": 2, "x": 3}}
    b = {"a": {"x": 3, "y": 2}, "b": 1}
    assert canonical_bytes(a) == canonical_bytes(b) == b'{"a":{"x":3,"y":2},"b":1}'


def test_non_ascii_is_escaped_not_localized() -> None:
    # ensure_ascii=True снимает вопрос NFC/NFD и «кто открыл файл в cp1251»
    assert canonical_bytes({"к": "да"}) == b'{"\\u043a":"\\u0434\\u0430"}'


def test_compact_separators() -> None:
    assert b", " not in canonical_bytes({"a": [1, 2], "b": "x"})


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_float_is_rejected(value: float) -> None:
    """NaN в хэшируемой записи — это будущая «целая цепочка, которая ничего не значит»."""
    with pytest.raises(Unrecordable):
        to_canonical_value({"x": value})


def test_float_becomes_fixed_scale_string() -> None:
    """repr(0.1+0.2) != '0.3'; договориться о строке проще, чем отлаживать расхождение хэшей."""
    assert to_canonical_value(0.1) == "0.100000"
    assert to_canonical_value(1 / 3) == "0.333333"


def test_decimal_keeps_its_own_scale() -> None:
    assert to_canonical_value(Decimal("0.10")) == "0.10"
    assert to_canonical_value(Decimal("1E+2")) == "100"


def test_int_overflow_rejected() -> None:
    with pytest.raises(Unrecordable):
        to_canonical_value(2**63)


def test_bytes_are_hex_prefixed() -> None:
    assert to_canonical_value(b"\x00\x01") == "hex:0001"


def test_dates_and_times_use_isoformat() -> None:
    assert to_canonical_value(date(2026, 1, 22)) == "2026-01-22"
    assert to_canonical_value(datetime(2026, 1, 22, 3, 4, 5)) == "2026-01-22T03:04:05"


def test_enum_collapses_to_value() -> None:
    class Kind(StrEnum):
        tool_run = "tool_run"

    assert to_canonical_value(Kind.tool_run) == "tool_run"


def test_path_becomes_str() -> None:
    assert to_canonical_value(PurePosixPath("/a/b")) == "/a/b"


def test_pydantic_model_goes_through_json_dump() -> None:
    class Args(BaseModel):
        query: str
        count: int = 5

    assert to_canonical_value(Args(query="курс")) == {"query": "курс", "count": 5}


def test_dataclass_and_namedtuple_are_supported() -> None:
    @dataclass
    class Point:
        x: int
        y: int

    from collections import namedtuple

    Vec = namedtuple("Vec", "a b")
    assert to_canonical_value(Point(1, 2)) == {"x": 1, "y": 2}
    assert to_canonical_value(Vec(1, 2)) == {"a": 1, "b": 2}


def test_sets_are_sorted_canonically() -> None:
    """Множество не упорядочено: без сортировки один и тот же набор давал бы разные хэши."""
    first = canonical_bytes({"tags": {"б", "а", "в"}})
    second = canonical_bytes({"tags": {"в", "а", "б"}})
    assert first == second


def test_dict_keys_are_stringified() -> None:
    assert to_canonical_value({1: "a", (2, 3): "b"}) == {"1": "a", "(2, 3)": "b"}


def test_unknown_object_is_refused() -> None:
    class Loose:
        pass

    with pytest.raises(Unrecordable, match="Loose"):
        to_canonical_value(Loose())


def test_hash_is_sha256_of_canonical_bytes() -> None:
    value = {"b": 1, "a": "x"}
    assert canonical_sha256(value) == sha256_bytes(canonical_bytes(value))
    assert len(canonical_sha256(value)) == 32
    assert sha256_hex(b"") == sha256_hex(b"") != sha256_hex(b"1")


def test_nested_unrecordable_propagates() -> None:
    with pytest.raises(Unrecordable):
        canonical_bytes({"a": [{"b": object()}]})
