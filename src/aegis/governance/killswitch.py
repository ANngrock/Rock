"""Kill switch: аварийная остановка записей по команде владельца.

Флаг живёт в Redis (а не в памяти процесса), поэтому переживает рестарт бота и виден всем
воркерам сразу. Активный флаг:

* запрещает любые write-действия (policy → DENY,см. :mod:`aegis.governance.policy`);
* не трогает read-only инструменты и слэш-команды — «деградация без LLM» продолжается.

Освобождается только владельцем (``/resume``), по таймасту не сгорает намеренно.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import orjson

from aegis.platform.kv import KV, killswitch_key

__all__ = ["KillSwitch", "KillSwitchState"]

KEY = killswitch_key()


@dataclass(slots=True)
class KillSwitchState:
    active: bool
    reason: str | None = None


class KillSwitch:
    def __init__(self, redis: KV, *, key: str = KEY) -> None:
        self._redis = redis
        self._key = key

    async def activate(self, reason: str = "вручную") -> None:
        payload = {"active": True, "reason": reason[:500]}
        await self._redis.set(self._key, orjson.dumps(payload))

    async def release(self) -> None:
        await self._redis.delete(self._key)

    async def state(self) -> KillSwitchState:
        raw = await self._redis.get(self._key)
        if not raw:
            return KillSwitchState(active=False)
        try:
            data: dict[str, Any] = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return KillSwitchState(active=True, reason="флаг повреждён")
        return KillSwitchState(active=bool(data.get("active")), reason=data.get("reason"))

    async def is_active(self) -> bool:
        return (await self.state()).active
