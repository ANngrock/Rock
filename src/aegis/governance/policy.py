"""Policy engine: ни один write не проходит без решения политики (принцип 2, ADR-006).

Ключевое отличие от «просьбы в промпте»: модель *не может* обойти это решение, потому что
``decide`` вызывается кодом между «tool_call получен» и «handler вызван». Промпт лишь *просит*
подтверждать; политика — *заставляет*.

Порядок правил (сверху вниз, первое совпадение выигрывает):

1. kill switch → DENY (даже для low-risk writes);
2. read-only → ALLOW;
3. write из untrusted-источника (web/чек/файл) → CONFIRM, всегда;
4. HIGH risk → CONFIRM;
5. MEDIUM risk или низкая уверенность извлечения → CONFIRM;
6. LOW risk и auto_allow_low_risk → ALLOW;
7. всё остальное → CONFIRM (deny-by-default на неизвестное).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from aegis.platform.config import Settings

__all__ = ["ActionContext", "Decision", "PolicyEngine", "Risk", "Trust"]

Trust = Literal["owner", "untrusted", "system"]


class Decision(StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


class Risk(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(slots=True)
class ActionContext:
    tool: str
    risk: Risk
    writes: bool
    source_trust: Trust
    args: dict[str, Any]
    confidence: float = 1.0
    kill_switch: bool = False
    idempotent: bool = False


class PolicyEngine:
    """Чистая функция без I/O — легко тестируется и переиспользуется (Temporal-activity, CLI)."""

    def __init__(self, auto_allow_low_risk: bool = True) -> None:
        self.auto_allow_low_risk = auto_allow_low_risk

    @classmethod
    def from_settings(cls, cfg: Settings) -> PolicyEngine:
        return cls(auto_allow_low_risk=cfg.auto_allow_low_risk)

    def decide(self, action: ActionContext) -> tuple[Decision, str]:
        if not action.writes:
            # read-only разрешён всегда: стоп про «не пиши», а не про «замолчи» — иначе /stop
            # ломает поиск и чтение заметок, то есть ровно то, что нужно во время инцидента
            return Decision.ALLOW, "read-only"
        if action.kill_switch:
            return Decision.DENY, "kill switch активен: записи приостановлены владельцем"
        if action.source_trust == "untrusted":
            return Decision.CONFIRM, "запрос на запись пришёл из внешнего контента"
        if action.risk is Risk.HIGH:
            return Decision.CONFIRM, "высокий риск"
        if action.risk is Risk.MEDIUM:
            return Decision.CONFIRM, "средний риск"
        if action.confidence < 0.8:
            return Decision.CONFIRM, "низкая уверенность извлечения"
        if action.risk is Risk.LOW and self.auto_allow_low_risk:
            return Decision.ALLOW, "низкий риск, авто-разрешено"
        if action.source_trust == "system" and action.idempotent:
            return Decision.ALLOW, "идемпотентная системная запись"
        return Decision.CONFIRM, "по умолчанию: подтвердить"
