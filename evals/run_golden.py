#!/usr/bin/env python3
"""Офлайн-прогон «золотого набора» v1.

Три вещи отделают evals от юнит-тестов:

* данные, а не код: новые кейсы добавляются строкой в ``golden_v1.jsonl`` (в т.ч. владельцем);
* прогон проверяет «контракт поведения» (роутинг/политика/DLP/рендер/инъекции) на реальных
  компонентах, без сети и без моделей — значит, он дёшево живёт в CI на каждый push;
* он же — база для шага 2, когда к этим же кейсам добавится прогон через LLM и Langfuse.

Запуск: ``python evals/run_golden.py`` (код возврата ≠ 0 при провале).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_SET = HERE / "golden_v1.jsonl"


@dataclass(slots=True)
class Case:
    id: str
    kind: str
    payload: dict[str, Any]


def load_cases(path: Path) -> list[Case]:
    cases: list[Case] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw = json.loads(line)
        cases.append(Case(id=str(raw["id"]), kind=str(raw["kind"]), payload=raw))
    return cases


# ------------------------------------------------------------------ checkers


def check_routing(case: Case) -> str | None:
    """Роутинг без моделей: проверяем детерминированное решение о роли/thinking/инструментах."""
    from aegis.agents.services import Services
    from aegis.agents.supervisor import Inbound, Supervisor
    from aegis.agents.tools.registry import Attachment, ToolRegistry
    from aegis.governance.policy import PolicyEngine
    from aegis.knowledge.notes import NoteHit
    from aegis.platform.config import Settings
    from aegis.platform.gateway.cost import CostGovernor
    from aegis.platform.gateway.dlp import DLP

    class _KV:
        async def get(self, key: str) -> bytes | None:
            return None

        async def set(self, key: str, value: Any, *, ex: int | None = None) -> None:
            return None

        async def delete(self, *keys: str) -> int:
            return 0

        async def getdel(self, key: str) -> bytes | None:
            return None

        async def incrbyfloat(self, key: str, amount: float) -> float:
            return amount

        async def expire(self, key: str, seconds: int) -> bool:
            return True

    class _Facts:
        async def recent(self, limit: int = 50) -> list[str]:
            return []

        async def list(self, limit: int = 50) -> list[Any]:
            return []

        async def add(self, *args: Any, **kwargs: Any) -> str:
            return "eval"

        async def invalidate(self, fact_id: str) -> bool:
            return False

    class _Notes:
        async def add(self, *args: Any, **kwargs: Any) -> Any:
            return None

        async def search(self, *args: Any, **kwargs: Any) -> list[NoteHit]:
            return []

    class _Gateway:
        def __init__(self, spent: float, limit: float) -> None:
            kv = _KV()
            self.dlp = DLP()
            self.cost = CostGovernor(kv, daily_limit_usd=limit)  # type: ignore[arg-type]

            async def seed() -> None:
                await self.cost.record(spent)

            self._seed = seed()

        async def chat(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("evals не должен дёргать модели")

        async def embed(self, *args: Any, **kwargs: Any) -> list[list[float]]:
            return []

        async def aclose(self) -> None:
            return None

    import asyncio

    async def run() -> Any:
        limit = 1.0
        spent = float(case.payload.get("spent_ratio", 0.0)) * limit
        gateway = _Gateway(spent, limit)
        await gateway._seed
        supervisor = Supervisor(
            services=Services(gateway=gateway, facts=_Facts(), notes=_Notes()),  # type: ignore[arg-type]
            registry=ToolRegistry(),
            policy=PolicyEngine(),
            kv=_KV(),  # type: ignore[arg-type]
            cfg=Settings(
                _env_file=None, _env_prefix="EVAL_", daily_budget_usd=limit, max_iterations=4
            ),
        )
        inbound = Inbound(
            text=str(case.payload.get("input", "")),
            owner_id=1,
            attachments=[
                Attachment(data=b"x", mime="image/jpeg")
                for _ in range(int(case.payload.get("attachments", 0)))
            ],
        )
        return await supervisor.route(inbound)

    route = asyncio.run(run())
    for key, want in case.payload["expect"].items():
        got = getattr(route, key)
        if bool(got) != bool(want):
            return f"{key}: ожидалось {want!r}, получено {got!r} (маршрут: {route.reason})"
    return None


def check_policy(case: Case) -> str | None:
    from aegis.governance.policy import ActionContext, PolicyEngine, Risk

    data = case.payload["input"]
    action = ActionContext(
        tool=str(data.get("tool", "t")),
        risk=Risk(str(data.get("risk", "none"))),
        writes=bool(data.get("writes", False)),
        source_trust=data.get("source_trust", "owner"),  # type: ignore[arg-type]
        args={},
        confidence=float(data.get("confidence", 1.0)),
        kill_switch=bool(data.get("kill_switch", False)),
    )
    decision, reason = PolicyEngine(
        auto_allow_low_risk=bool(case.payload.get("auto_allow_low_risk", True))
    ).decide(action)
    want = case.payload["expect"]
    if str(decision) != want:
        return f"ожидалось {want}, получено {decision} ({reason})"
    if not reason:
        return "решение без объяснения"
    return None


def check_dlp(case: Case) -> str | None:
    from aegis.platform.gateway.dlp import DLP

    masked, mapping = DLP().mask(str(case.payload["input"]))
    for needle in case.payload.get("expect_absent", []):
        if str(needle).replace(" ", "") in masked.replace(" ", ""):
            return f"PII {needle!r} осталось в маске"
    for needle in case.payload.get("expect_contains", []):
        if str(needle) not in masked:
            return f"в маске нет маркера {needle!r}"
    return None


def check_dlp_roundtrip(case: Case) -> str | None:
    from aegis.platform.gateway.dlp import DLP

    source = str(case.payload["input"])
    masked, mapping = DLP().mask(source)
    restored = DLP().unmask(masked, mapping)
    return None if restored == source else f"roundtrip сломан: {restored!r}"


def check_injection(case: Case) -> str | None:
    from aegis.web.search import wrap_untrusted

    wrapped = wrap_untrusted("eval", str(case.payload["input"]))
    expect = case.payload["expect"]
    if wrapped.count("<untrusted") != int(expect["single_open"]):
        return f"открывающих тегов: {wrapped.count('<untrusted')}"
    if wrapped.count("</untrusted>") != int(expect["single_close"]):
        return "внешний текст прорвал контейнер untrusted"
    if expect.get("contains_text") and "rm -rf" in wrapped and wrapped.count("rm -rf") != 1:
        return "инъекция задублировалась"
    return None


def check_prompt(case: Case) -> str | None:
    from aegis.agents.prompts.system import build_system_prompt
    from aegis.agents.tools import builtin  # noqa: F401
    from aegis.agents.tools.registry import registry

    prompt = build_system_prompt(
        "Europe/Moscow",
        "RUB",
        ["владелец не пьёт кофе после 16"],
        [(t.name, t.description) for t in registry.all()],
    )
    for needle in case.payload["expect_contains"]:
        if str(needle) not in prompt:
            return f"в промпте нет {needle!r}"
    return None


def check_render(case: Case) -> str | None:
    from aegis.interaction.telegram.render import sanitize_html

    safe, _ = sanitize_html(str(case.payload["input"]))
    for needle in case.payload.get("expect_contains", []):
        if str(needle) not in safe:
            return f"в выводе нет {needle!r}: {safe[:120]!r}"
    for needle in case.payload.get("expect_absent", []):
        if str(needle) in safe:
            return f"в выводе осталось {needle!r}"
    return None


def check_render_chunks(case: Case) -> str | None:
    from aegis.interaction.telegram.render import chunk_html, sanitize_html

    text, _ = sanitize_html(
        "абзац с <b>разметкой</b> и текстом " * int(case.payload["input_repeat"] // 30)
    )
    chunks = chunk_html(text)
    limit = 4096
    if any(len(c) > limit for c in chunks):
        return f"чанк длиннее лимита: {max(len(c) for c in chunks)}"
    if any(c.count("<b>") != c.count("</b>") for c in chunks):
        return "несбалансированная разметка внутри чанка"
    return None


def check_intent(case: Case) -> str | None:
    """Что перехватывает детерминированный путь — офлайн, без источников и без моделей.

    Отдельный kind именно потому, что здесь важен сам факт: «это вопрос с точным ответом в
    первоисточнике» решает код, а не настроение модели.
    """
    from aegis.web.rates import parse_rate_question

    question = parse_rate_question(str(case.payload.get("input", "")))
    expect = case.payload.get("expect", {})
    got: dict[str, object] = {
        "mode": question.mode if question else None,
        "base": question.base if question else None,
        "quote": question.quote if question else None,
        "cash": question.cash if question else None,
    }
    for key, want in expect.items():
        if got.get(key) != want:
            return f"{key}: ожидалось {want!r}, получилось {got.get(key)!r}"
    return None


CHECKERS = {
    "routing": check_routing,
    "intent": check_intent,
    "policy": check_policy,
    "dlp": check_dlp,
    "dlp_roundtrip": check_dlp_roundtrip,
    "injection": check_injection,
    "prompt": check_prompt,
    "render": check_render,
    "render_chunks": check_render_chunks,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="прогон золотого набора aegis (офлайн)")
    parser.add_argument("--set", dest="dataset", default=str(DEFAULT_SET), type=Path)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    cases = load_cases(args.dataset)
    failures: list[tuple[str, str]] = []
    passed = 0
    for case in cases:
        checker = CHECKERS.get(case.kind)
        if checker is None:
            failures.append((case.id, f"неизвестный kind {case.kind!r}"))
            continue
        try:
            problem = checker(case)
        except Exception as exc:  # noqa: BLE001 - падение проверки = провал кейса
            problem = f"{type(exc).__name__}: {exc}"
        if problem is None:
            passed += 1
            if args.verbose:
                print(f"  ok   {case.id} ({case.kind})")
        else:
            failures.append((case.id, problem))
            print(f"  FAIL {case.id} ({case.kind}): {problem}", file=sys.stderr)

    total = len(cases)
    print(
        f"golden_v1: {passed}/{total} кейсов пройдено"
        + (f", провалов: {len(failures)}" if failures else "")
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
