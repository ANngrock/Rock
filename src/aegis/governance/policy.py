"""Policy engine: ни один write не проходит без решения политики (принцип 2, ADR-006).

Ключевое отличие от «просьбы в промпте»: модель *не может* обойти это решение, потому что
``decide`` вызывается кодом между «tool_call получен» и «handler вызван». Промпт лишь *просит*
подтверждать; политика — *заставляет*.

Порядок правил (сверху вниз, первое совпадение выигрывает):

1. kill switch → DENY (даже для low-risk writes);
2. read-only → ALLOW;
3. write без гранта принципала (RBAC, F2) → DENY;
4. write из untrusted-источника (web/чек/файл) → CONFIRM, всегда;
5. личный бюджет исчерпан → DENY;
6. HIGH risk → CONFIRM;
7. MEDIUM risk или низкая уверенность извлечения → CONFIRM;
8. LOW risk и auto_allow_low_risk → ALLOW;
9. всё остальное → CONFIRM (deny-by-default на неизвестное).

С F5 этот порядок живёт ДАННЫМИ (:mod:`aegis.governance.policy_rules`): файл
``deploy/policy/rules.yml`` версионируется, каждое решение несёт ``rule_id@version``, а новый
набор правил можно прогнать тенью по журналу до включения. Без файла движок использует
встроенный набор — поведение побайтово то же, что было до переезда в данные (принцип 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from aegis.governance.policy_rules import Rule, RuleSet, default_ruleset, load_ruleset

if TYPE_CHECKING:
    from aegis.platform.config import Settings

__all__ = ["ActionContext", "Decision", "DecisionOutcome", "PolicyEngine", "Risk", "Trust"]

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
    #: RBAC (F2): какое действие требует инструмент и выдано ли оно принципалу.
    #  По умолчанию «выдано» — совместимость со старыми вызовами и тестами, где принципов нет
    required_action: str = ""
    permission_missing: bool = False
    #: «кто спросил не владелец» и доля личного бюджета (F2/F7) — условия правил
    non_owner: bool = False
    budget_ratio: float = 0.0


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    """Решение + объяснение + тег правила. ``decide`` возвращает пару, ``decide_full`` — это.

    Тег (``killswitch@1``) нужен не для красоты: без него «правило изменили к лучшему»
    проверяется разговорами, а с ним — пересчётом журнала (shadow в ``aegis policy shadow``).
    """

    decision: Decision
    reason: str
    rule: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"decision": str(self.decision), "reason": self.reason, "rule": self.rule or None}


class PolicyEngine:
    """Чистая функция без I/O — легко тестируется и переиспользуется (Temporal-activity, CLI).

    Порядок evals/CI завязан на то, что без файла правил решения идентичны историческим:
    встроенный набор — тот же порядок, записанный данными.
    """

    def __init__(self, auto_allow_low_risk: bool = True, ruleset: RuleSet | None = None) -> None:
        self.auto_allow_low_risk = auto_allow_low_risk
        self.ruleset: RuleSet = ruleset or default_ruleset()

    @classmethod
    def from_settings(cls, cfg: Settings) -> PolicyEngine:
        engine = cls(auto_allow_low_risk=cfg.auto_allow_low_risk)
        try:
            loaded = load_ruleset(getattr(cfg, "policy_rules_path", ""))
        except Exception as exc:  # noqa: BLE001 — плохой файл не должен ронять бота
            from aegis.governance.policy_rules import PolicyFileError  # noqa: PLC0415

            if isinstance(exc, PolicyFileError):
                import structlog  # noqa: PLC0415

                structlog.get_logger(__name__).error("policy.rules_invalid", err=str(exc)[:300])
            else:
                raise
        else:
            if loaded is not None:
                engine.ruleset = loaded
        return engine

    @property
    def ruleset_name(self) -> str:
        return f"{self.ruleset.name}:{self.ruleset.sha()}"

    def decide_full(self, action: ActionContext) -> DecisionOutcome:
        ctx = {
            "tool": action.tool,
            "risk": str(action.risk),
            "writes": action.writes,
            "source_trust": action.source_trust,
            "confidence": action.confidence,
            "kill_switch": action.kill_switch,
            "idempotent": action.idempotent,
            "permission_missing": action.permission_missing,
            "non_owner": action.non_owner,
            "budget_ratio": action.budget_ratio,
            "required_action": action.required_action,
            "auto_allow_low_risk": self.auto_allow_low_risk,
        }
        rule: Rule | None
        rule, reason = self.ruleset.evaluate(ctx)
        decision = Decision(rule.verdict) if rule is not None else Decision.DENY
        return DecisionOutcome(decision, reason, rule.tag if rule is not None else "")

    def decide(self, action: ActionContext) -> tuple[Decision, str]:
        outcome = self.decide_full(action)
        return outcome.decision, outcome.reason
