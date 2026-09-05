"""Офлайн-золото воспроизводимости: хэши канонизации, цепочки и промптов.

Зачем это отдельным набором, а не юнит-тестами: тесты проверяют *логику* (цепочка сходится,
подмена ловится). Здесь закреплены *сами байты*. Если канонизация или текст промпта изменятся,
старые журналы перестанут сходиться с новыми пересчётами — и владелец узнаёт об этом здесь, в CI,
а не на `aegis repro verify` через полгода, когда «поломанный» журнал окажется целым, просто
посчитанным по-другому.

Прогоном только кода: фиксируются канон-байты записей, их хэши, корень Меркла за день, хэши
файлов промптов. Случайных данных нет — вход детерминирован, поэтому расхождение означает ровно
одно: изменился расчёт или текст.

    python evals/run_repro.py            # сверить с эталоном
    python evals/run_repro.py --write    # переписать эталон (осознанно, с диффом в коммите)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import orjson

from aegis.governance.recorder import (
    link_hash,
    merkle_root,
    record_payload,
)
from aegis.platform.canonical import canonical_bytes
from aegis.platform.prompts import index, load

__all__ = ["cases", "main"]

ROOT = Path(__file__).resolve().parent
GOLDEN = ROOT / "repro_journal_v1.jsonl"

_PROMPT_IDS = ("repro/judge",)


def _records() -> list[dict[str, Any]]:
    """Набор записей, покрывающий все kind'ы и все «опасные» типы значений."""
    zero = bytes(32)
    base: dict[str, Any] = {
        "id": "00000000-0000-0000-0000-000000000001",
        "trace_id": "11111111-1111-1111-1111-111111111111",
        "turn_no": 1,
        "owner_id": 1,
        "prompt_ids": [{"id": "core/system", "version": "sys-v0.3.0", "sha256": "a" * 64}],
        "tools_schema_sha": None,
        "model": "glm-4.7",
        "params": {},
        "input_sha": None,
        "output_sha": None,
        "policy": None,
        "cost_usd": "0.001230",
        "latency_ms": 777,
        "truncated": False,
        "note": None,
    }
    specs: list[tuple[str, dict[str, Any]]] = [
        ("llm_call", {"kind": "llm_call", "params": {"role": "brain", "prompt_tokens": 120}}),
        (
            "policy_allow",
            {
                "kind": "policy",
                "params": {"tool": "add_note", "risk": "low"},
                "policy": {"decision": "allow", "reason": "низкий риск"},
            },
        ),
        (
            "policy_confirm",
            {
                "kind": "policy",
                "params": {"tool": "pay", "risk": "high"},
                "policy": {"decision": "confirm", "reason": "write + high"},
            },
        ),
        (
            "policy_deny",
            {
                "kind": "policy",
                "params": {"tool": "pay", "risk": "high"},
                "policy": {"decision": "deny", "reason": "kill switch"},
            },
        ),
        (
            "tool_run_untrusted",
            {
                "kind": "tool_run",
                "params": {"tool": "web_search", "risk": "none", "trust": "untrusted"},
                "truncated": False,
                "note": "поиск внешнего текста",
            },
        ),
        (
            "turn_summary",
            {
                "kind": "turn_summary",
                "params": {"iterations": 2, "route": "brain:tools"},
                "tools_schema_sha": bytes(range(32)),
                "input_sha": bytes([7]) * 32,
                "output_sha": bytes([9]) * 32,
                "truncated": True,
            },
        ),
    ]
    out: list[dict[str, Any]] = []
    prev = zero
    for number, (name, over) in enumerate(specs, start=1):
        payload = {
            **base,
            **over,
            "turn_no": number,
            "id": f"{number:08d}-0000-0000-0000-000000000000",
        }
        record = {**payload, "prev_hash": prev}
        digest = link_hash(prev, record)
        out.append({"name": name, "record": record, "hash": digest.hex()})
        prev = digest
    return out


def cases() -> list[dict[str, Any]]:
    """Эталонные значения: запись → канон-байты → хэш; день → merkle root; промпт → sha256."""
    out: list[dict[str, Any]] = []
    records = _records()
    for item in records:
        record = item["record"]
        out.append(
            {
                "case": f"canonical:{item['name']}",
                "kind": "canonical",
                "value": canonical_bytes(record_payload(record)).decode(),
            }
        )
        out.append({"case": f"hash:{item['name']}", "kind": "hash", "value": item["hash"]})
    leaves = [bytes.fromhex(item["hash"]) for item in records]
    out.append({"case": "merkle:day", "kind": "hash", "value": merkle_root(leaves).hex()})
    out.append({"case": "merkle:empty-day", "kind": "hash", "value": merkle_root([]).hex()})
    for prompt_id in _PROMPT_IDS:
        prompt = load(prompt_id)
        out.append(
            {
                "case": f"prompt:{prompt.id}",
                "kind": "prompt",
                "value": prompt.sha256,
                "version": prompt.version,
            }
        )
    listed = sorted(index().keys())
    out.append({"case": "prompts:index", "kind": "list", "value": ",".join(listed)})
    return out


def _read_golden() -> dict[str, dict[str, Any]]:
    if not GOLDEN.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = orjson.loads(line)
        out[str(item["case"])] = item
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="золото воспроизводимости (M1)")
    parser.add_argument("--write", action="store_true", help="перезаписать эталон")
    args = parser.parse_args(argv)

    produced = cases()
    if args.write:
        GOLDEN.write_text(
            "\n".join(orjson.dumps(item).decode() for item in produced) + "\n", encoding="utf-8"
        )
        print(f"эталон записан: {GOLDEN.name} ({len(produced)} проверок)")
        return 0

    golden = _read_golden()
    if not golden:
        print("! нет эталона: `python evals/run_repro.py --write`", file=sys.stderr)
        return 2
    bad: list[str] = []
    for item in produced:
        expected = golden.get(item["case"])
        if expected is None:
            bad.append(f"{item['case']}: появился новый случай — обнови эталон")
        elif expected.get("value") != item["value"]:
            bad.append(
                f"{item['case']}: расхождение\n    было: {expected.get('value')}"
                f"\n    стало: {item['value']}"
            )
    for name in golden:
        if name not in {item["case"] for item in produced}:
            bad.append(f"{name}: случай исчез из прогонов — кто-то удалил запись из набора")
    if bad:
        print(
            f"repro-золото: {len(bad)} расхождений (журнал перестанет сходиться с прошими записями)"
        )
        for line in bad:
            print(" -", line)
        return 1
    print(f"repro-золото: {len(produced)}/{len(produced)} совпало")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
