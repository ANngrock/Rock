"""Cost governor: бюджет считается по дню владельца, деградация — по долям лимита."""

from __future__ import annotations

import pytest

from aegis.platform.gateway.cost import BudgetExceeded, CostGovernor
from conftest import FakeKV


@pytest.fixture
def kv() -> FakeKV:
    return FakeKV()


def governor(kv: FakeKV, limit: float = 10.0, tz: str = "Europe/Moscow") -> CostGovernor:
    return CostGovernor(kv, daily_limit_usd=limit, timezone=tz)  # type: ignore[arg-type]


async def test_record_and_spent(kv: FakeKV) -> None:
    gov = governor(kv)
    assert await gov.spent() == 0.0
    await gov.record(0.25)
    await gov.record(0.5)
    assert await gov.spent() == pytest.approx(0.75)
    ttls = {k[4:]: v for k, v in kv.data.items() if k.startswith("ttl:")}
    assert ttls and all(int(v) == 60 * 60 * 24 * 3 for v in ttls.values()), "ключ обязан протухать"


async def test_zero_and_negative_costs_are_clamped(kv: FakeKV) -> None:
    gov = governor(kv)
    await gov.record(-1.0)
    assert await gov.spent() == 0.0


async def test_check_blocks_before_the_request(kv: FakeKV) -> None:
    gov = governor(kv, limit=1.0)
    await gov.record(0.995)
    with pytest.raises(BudgetExceeded):
        await gov.check(0.01)


async def test_check_passes_while_budget_remains(kv: FakeKV) -> None:
    await governor(kv, limit=1.0).check(0.01)


async def test_degradation_levels(kv: FakeKV) -> None:
    gov = governor(kv, limit=1.0)
    assert gov.degradation_level(0.0) == 0
    assert gov.degradation_level(0.59) == 0
    assert gov.degradation_level(0.60) == 1
    assert gov.degradation_level(0.84) == 1
    assert gov.degradation_level(0.85) == 2
    assert gov.degradation_level(3.0) == 2


async def test_day_key_follows_owner_timezone(kv: FakeKV) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    gov = governor(kv, tz="Asia/Kamchatka")
    await gov.record(0.1)
    expected = datetime.now(ZoneInfo("Asia/Kamchatka")).date().isoformat()
    assert any(expected in key for key in kv.data), "бюджетный день должен начинаться у владельца"


async def test_snapshot_shape(kv: FakeKV) -> None:
    gov = governor(kv, limit=2.0)
    await gov.record(0.5)
    snap = await gov.snapshot()
    assert snap["limit_usd"] == 2.0
    assert snap["spent_usd"] == pytest.approx(0.5)
    assert snap["ratio"] == pytest.approx(0.25)
    assert snap["degradation_level"] == 0


def test_invalid_limit_rejected(kv: FakeKV) -> None:
    with pytest.raises(ValueError):
        governor(kv, limit=0)


def test_key_is_scoped_by_day(kv: FakeKV) -> None:
    gov = governor(kv)
    assert gov._key.startswith("cost:day:")
