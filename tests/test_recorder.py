"""Журнал решений: хэш-цепочка и сверка — чистые функции, без БД.

DB-часть (вставка, INSTEAD OF-триггер, блобы, якорь) проверяется живым прогоном:
``tests/integration/test_repro.py``. Здесь — то, что обязано быть верным при любых данных: как
считается связь, что считается подменой и почему «выключенный журнал» отличается от «пустого».
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.governance.recorder import (
    BlobRef,
    ChainReport,
    NullDecisionRecorder,
    check_chain,
    link_hash,
    merkle_root,
    record_payload,
)


def _record(seq: int, prev: bytes, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": f"00000000-0000-0000-0000-{seq:012d}",
        "trace_id": "11111111-1111-1111-1111-111111111111",
        "turn_no": 1,
        "kind": "llm_call",
        "owner_id": 1,
        "prompt_ids": [],
        "tools_schema_sha": None,
        "model": "glm-4.7",
        "params": {"role": "brain"},
        "input_sha": None,
        "output_sha": None,
        "policy": None,
        "cost_usd": "0.001234",
        "latency_ms": 42,
        "truncated": False,
        "note": None,
        "prev_hash": prev,
        **fields,
    }
    payload["hash"] = link_hash(prev, payload)
    return {**payload, "seq": seq}


def test_link_hash_depends_on_prev() -> None:
    base = _record(1, bytes(32))
    other = _record(1, bytes([1] + [0] * 31))
    assert base["hash"] != other["hash"], "цепочка, не связанная prev_hash, — это список записей"


def test_record_payload_normalises_money_and_shas() -> None:
    payload = record_payload(
        {"cost_usd": 0.5, "prev_hash": b"\x00" * 32, "tools_schema_sha": None, "seq": 7}
    )
    assert payload["cost_usd"] == "0.500000"
    assert payload["prev_hash"] == "00" * 32
    assert payload["tools_schema_sha"] is None
    assert "seq" not in payload, "seq — состояние БД, он не должен входить в хэш"
    assert "created_at" not in payload


def test_chain_of_three_verifies() -> None:
    first = _record(1, bytes(32))
    second = _record(2, first["hash"], kind="tool_run", params={"tool": "web_search"})
    third = _record(3, second["hash"])
    report = check_chain([first, second, third])
    assert report.ok, report.problems
    assert (report.checked, report.first_seq, report.last_seq) == (3, 1, 3)


def test_edited_field_breaks_the_hash() -> None:
    first = _record(1, bytes(32))
    second = _record(2, first["hash"])
    tampered = {**second, "cost_usd": "0.000000"}  # «уменьшили» стоимость, пересчитав только строку
    report = check_chain([first, tampered])
    assert not report.ok
    assert any("хэш не совпадает" in problem for problem in report.problems)


def test_removed_row_is_reported_as_gap() -> None:
    first = _record(1, bytes(32))
    third = _record(3, first["hash"])  # seq 2 удалён, а ссылку left'а никто не переставлял
    report = check_chain([first, third])
    assert not report.ok
    assert report.gaps == 1
    assert "удалял строки" in report.summary()


def test_broken_link_is_named_by_seq() -> None:
    first = _record(1, bytes(32))
    second = _record(2, bytes(32))  # не на первую ссылается
    report = check_chain([first, second])
    assert not report.ok and any("prev_hash" in p for p in report.problems)


def test_window_not_starting_at_head_is_not_corruption() -> None:
    """«Смотрим с середины» и «цепочку порвали» — разные диагнозы: первое не повод паниковать."""
    first = _record(1, bytes(32))
    second = _record(2, first["hash"])
    report = check_chain([second])
    assert not report.ok  # проверить ссылку нечем
    assert any("смотрите цепочку с начала" in problem for problem in report.problems)
    assert report.gaps == 0


def test_empty_report_says_there_is_nothing_to_replay() -> None:
    assert "записей нет" in ChainReport(ok=True, checked=0).summary()


def test_merkle_root_is_order_sensitive_and_stable() -> None:
    leaves = [bytes([i]) * 32 for i in (1, 2, 3)]
    assert merkle_root(leaves) == merkle_root(list(leaves))
    assert merkle_root(leaves) != merkle_root(list(reversed(leaves)))
    assert len(merkle_root(leaves)) == 32


def test_merkle_empty_day_has_a_root_of_its_own() -> None:
    """Пустой день — не «ноль-байтовый корень», а отдельное значение: якорь «дня без записей»
    должен отличаться от якоря «мы ничего не посчитали»."""
    from aegis.platform.canonical import sha256_bytes

    assert merkle_root([]) == sha256_bytes(b"aegis:empty-day")


def test_merkle_odd_tail_is_duplicated_not_dropped() -> None:
    """Нечётный уровень дублирует последний лист (детерминированно) — иначе он бы «испарился»."""
    from aegis.platform.canonical import sha256_bytes

    leaf = bytes([7]) * 32
    assert merkle_root([leaf]) == leaf  # один лист = корень
    assert merkle_root([leaf, leaf]) == sha256_bytes(leaf + leaf)
    odd = [bytes([i]) * 32 for i in range(5)]
    assert merkle_root(odd) == merkle_root([*odd[:4], odd[4], odd[4]][:5])


def test_blob_ref_hex_is_display_form() -> None:
    ref = BlobRef(sha256=bytes(range(32)), size=10)
    assert len(ref.hex) == 64 and ref.hex.startswith("000102")
    assert ref.truncated is False


@pytest.mark.asyncio
async def test_null_recorder_is_disabled_but_answerable() -> None:
    """Выключенный журнал обязан отвечать, а не молчать: пустой ответ модель заполнит догадками."""
    recorder = NullDecisionRecorder()
    assert recorder.enabled is False
    recorder.begin_turn("trace", owner_id=1)  # begin/end — синхронные: hot path не ждёт БД
    recorder.end_turn("trace")
    await recorder.on_llm_call(object())
    await recorder.tool_run(trace_id="t")
    await recorder.turn_summary(trace_id="t")
    assert (await recorder.records_for_trace("11111111-1111-1111-1111-111111111111")) == []
    assert await recorder.latest_trace(owner_id=1) is None
    assert await recorder.matching_traces("111111") == []
    assert await recorder.record_input({}) is None
    assert (await recorder.stats())["enabled"] is False
    report = await recorder.verify()
    assert not report.ok and any("нет БД" in problem for problem in report.problems)
    anchor = await recorder.anchor("2026-01-22")
    assert not anchor.ok and "выключена" in anchor.note
