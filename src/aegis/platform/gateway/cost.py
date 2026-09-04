"""Кост-контроль: дневной бюджет LLM и ступенчатая деградация.

Бюджет считается по локальному дню владельца (не UTC) — «$2 в день» значит «в моём дне».
Счётчик живёт в Redis с TTL на 3 дня: он переживает рестарт процесса и остаётся видимым
из любой реплики бота.

Деградация по мере роста затрат (принцип «дешевле, но работает»):

* уровень 0 (< 60 % бюджета) — штатный режим;
* уровень 1 (60–85 %) — thinking выключается, vision — только по явной просьбе;
* уровень 2 (> 85 %) — brain-запросы уходят на fast-модель.

Потолок бюджета — жёсткий: :class:`BudgetExceeded` поднимается *до* запроса, то есть
перерасход невозможен в пределах одной итерации агента.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aegis.platform.kv import KV

__all__ = ["BudgetExceeded", "CostGovernor"]

_DEGRADATION_SOFT = 0.6
_DEGRADATION_HARD = 0.85


class BudgetExceeded(RuntimeError):
    """Дневной бюджет исчерпан — LLM-вызовы запрещены, команды продолжают работать."""


class CostGovernor:
    def __init__(
        self,
        redis: KV,
        daily_limit_usd: float,
        *,
        timezone: str = "UTC",
        key_prefix: str = "cost",
    ) -> None:
        if daily_limit_usd <= 0:
            raise ValueError("daily_limit_usd должен быть > 0")
        self._redis = redis
        self.limit = float(daily_limit_usd)
        self._tz = timezone
        self._prefix = key_prefix

    # --- ключи ---

    @property
    def _key(self) -> str:
        return f"{self._prefix}:day:{self._today}"

    @property
    def _today(self) -> str:
        try:
            from zoneinfo import ZoneInfo

            now = datetime.now(ZoneInfo(self._tz))
        except Exception:  # noqa: BLE001 - битая TZ не должна валить учёт затрат
            now = datetime.now()
        return now.date().isoformat()

    # --- учёт ---

    async def spent(self) -> float:
        raw = await self._redis.get(self._key)
        if raw is None:
            return 0.0
        return float(raw.decode() if isinstance(raw, bytes) else raw)

    async def record(self, cost_usd: float) -> float:
        key = self._key
        total = await self._redis.incrbyfloat(key, max(cost_usd, 0.0))
        await self._redis.expire(key, 60 * 60 * 24 * 3)
        return float(total)

    async def check(self, estimate_usd: float = 0.01) -> None:
        """Поднять BudgetExceeded, если следующий вызов не влезает в бюджет."""
        if await self.spent() + estimate_usd > self.limit:
            raise BudgetExceeded(
                f"дневной бюджет ${self.limit:.2f} исчерпан (потрачено ${await self.spent():.3f})"
            )

    # --- планы/метрики ---

    def degradation_level(self, spent: float) -> int:
        """0 — норма, 1 — отключить thinking, 2 — только fast-модель."""
        ratio = spent / self.limit
        if ratio >= _DEGRADATION_HARD:
            return 2
        if ratio >= _DEGRADATION_SOFT:
            return 1
        return 0

    async def snapshot(self) -> dict[str, Any]:
        spent = await self.spent()
        return {
            "day": self._today,
            "spent_usd": round(spent, 4),
            "limit_usd": round(self.limit, 4),
            "ratio": round(spent / self.limit, 3),
            "degradation_level": self.degradation_level(spent),
        }
