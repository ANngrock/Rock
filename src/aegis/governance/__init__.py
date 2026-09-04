"""Governance: policy engine, kill switch, аудит.

Здесь нет знаний о доменах и о том, *что именно* делает агент — только про то, *разрешено ли*
это делать и как это потом найти в аудите.
"""

from __future__ import annotations

from aegis.governance.killswitch import KillSwitch, KillSwitchState
from aegis.governance.policy import ActionContext, Decision, PolicyEngine, Risk, Trust

__all__ = [
    "ActionContext",
    "Decision",
    "KillSwitch",
    "KillSwitchState",
    "PolicyEngine",
    "Risk",
    "Trust",
]
