"""Policy engine: решение о каждом действии. Чистая функция — тестируется без I/O."""

from __future__ import annotations

import pytest

from aegis.governance.policy import ActionContext, Decision, PolicyEngine, Risk


def ctx(**kw: object) -> ActionContext:
    base: dict[str, object] = {
        "tool": "t",
        "risk": Risk.NONE,
        "writes": False,
        "source_trust": "owner",
        "args": {},
    }
    return ActionContext(**{**base, **kw})  # type: ignore[arg-type]


def test_read_only_is_allowed() -> None:
    assert PolicyEngine().decide(ctx())[0] is Decision.ALLOW


def test_untrusted_write_requires_confirmation() -> None:
    decision, reason = PolicyEngine().decide(
        ctx(writes=True, risk=Risk.LOW, source_trust="untrusted")
    )
    assert decision is Decision.CONFIRM
    assert "внешн" in reason


def test_kill_switch_denies_everything_that_writes() -> None:
    assert (
        PolicyEngine().decide(ctx(writes=True, risk=Risk.LOW, kill_switch=True))[0] is Decision.DENY
    )


def test_kill_switch_does_not_block_reads() -> None:
    # важно: kill switch = «не менять данные», а не «молчать»
    assert PolicyEngine().decide(ctx(kill_switch=True))[0] is Decision.ALLOW


@pytest.mark.parametrize("risk", [Risk.MEDIUM, Risk.HIGH])
def test_medium_and_high_risk_confirm(risk: Risk) -> None:
    assert PolicyEngine().decide(ctx(writes=True, risk=risk))[0] is Decision.CONFIRM


def test_low_risk_auto_allow_can_be_switched_off() -> None:
    strict = PolicyEngine(auto_allow_low_risk=False)
    assert strict.decide(ctx(writes=True, risk=Risk.LOW))[0] is Decision.CONFIRM
    assert (
        PolicyEngine(auto_allow_low_risk=True).decide(ctx(writes=True, risk=Risk.LOW))[0]
        is Decision.ALLOW
    )


def test_low_confidence_needs_confirmation() -> None:
    decision, reason = PolicyEngine().decide(ctx(writes=True, risk=Risk.LOW, confidence=0.5))
    assert decision is Decision.CONFIRM
    assert "уверенность" in reason


def test_system_idempotent_write_allowed() -> None:
    assert (
        PolicyEngine(auto_allow_low_risk=False).decide(
            ctx(writes=True, risk=Risk.LOW, source_trust="system", idempotent=True)
        )[0]
        is Decision.ALLOW
    )


def test_reasons_are_never_empty() -> None:
    """Каждое решение объяснимо — это то, что читает владелец в сообщении подтверждения."""
    engine = PolicyEngine()
    for risk in Risk:
        for trust in ("owner", "untrusted", "system"):
            for writes in (True, False):
                decision, reason = engine.decide(ctx(risk=risk, writes=writes, source_trust=trust))  # type: ignore[arg-type]
                assert reason
                assert decision in (Decision.ALLOW, Decision.CONFIRM, Decision.DENY)
