"""Политика как данные (F5): декларативные правила с id, версией и тенью на журнале.

Чего не хватало: ``policy.py`` умел объяснить *почему запрещено* (строка reason), но не имел
способа ответить «стало ли лучше после изменения правила». Здесь:

* правила живут в версионируемом файле (``deploy/policy/rules.yml``): id, версия, условие,
  вердикт, reason-шаблон;
* в решение пишется ``rule_id@version`` — поле ``policy`` журнала существовало всегда, теперь там
  есть на что опереться;
* **shadow-оценка**: новый набор правил прогоняется по последним N тысячам ходов *до* включения и
  печатает «что изменилось бы». Ослабление без golden-кейса не проходит гейт;
* порядок правил = приоритет, первое совпадение побеждает — ровно как в коде раньше; дефолтный
  набор (:func:`default_ruleset`) — тот же самый порядок, записанный данными, поэтому «нет
  файла» и «файл = дефолт» дают побайтово одинаковые решения.

Модуль чистый: YAML разбирается здесь, БД не трогается (shadow читает журнал отдельным
репозиторием). Это то, за что стоит платить отдельным файлом: правила проверяются без
поднятого стека, а evals вызывают их так же, как продакшен.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson
import structlog

__all__ = [
    "PolicyFileError",
    "Rule",
    "RuleSet",
    "default_ruleset",
    "load_ruleset",
    "lock_payload",
    "plan_change_vs_lock",
    "verdict_rank",
]

log = structlog.get_logger(__name__)

_VERDICTS = ("allow", "confirm", "deny")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,60}$")

#: поля ActionContext, на которые может смотреть условие. Всё, чего здесь нет, — ошибка файла,
#: а не «тихоигнорируемое поле»: молча проигнорированное условие означает «правило шире, чем
#: казалось автору», то есть ровно ту деградацию, против которой этот слой заводится
_CONDITION_KEYS = frozenset(
    {
        "writes",
        "kill_switch",
        "source_trust",
        "risk_in",
        "confidence_lt",
        "confidence_gte",
        "idempotent",
        "tool",
        "tool_glob",
        "non_owner",
        "permission_missing",
        "budget_ratio_gte",
        "always",
        "auto_allow_low_risk",
    }
)


class PolicyFileError(ValueError):
    """Плохой файл правил: падаем на старте, а не в первом же write-инструменте."""


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    version: int
    when: Mapping[str, Any]
    verdict: str
    reason: str = ""
    note: str = ""

    @property
    def tag(self) -> str:
        return f"{self.id}@{self.version}"

    def matches(self, ctx: Mapping[str, Any]) -> bool:
        """Все условия конъюнкцией; любое неизвестное условие в файле — ошибка загрузки, не
        здесь."""
        for key, want in self.when.items():
            if key == "always":
                if bool(want) is not True:
                    return False
            elif key == "tool":
                if str(ctx.get("tool", "")) != str(want):
                    return False
            elif key == "tool_glob":
                if not any(
                    fnmatch.fnmatch(str(ctx.get("tool", "")), str(pattern)) for pattern in want
                ):
                    return False
            elif key == "source_trust":
                if str(ctx.get("source_trust", "owner")) not in {str(v) for v in want}:
                    return False
            elif key == "risk_in":
                if str(ctx.get("risk", "none")) not in {str(v) for v in want}:
                    return False
            elif key == "confidence_lt":
                if float(ctx.get("confidence", 1.0)) >= float(want):
                    return False
            elif key == "confidence_gte":
                if float(ctx.get("confidence", 1.0)) < float(want):
                    return False
            elif key == "budget_ratio_gte":
                if float(ctx.get("budget_ratio", 0.0)) < float(want):
                    return False
            elif key == "auto_allow_low_risk":
                if bool(ctx.get("auto_allow_low_risk", True)) is not bool(want):
                    return False
            elif key in ("writes", "kill_switch", "idempotent", "non_owner", "permission_missing"):
                if bool(ctx.get(key, False)) is not bool(want):
                    return False
            else:  # pragma: no cover — защита от рассинхрона с _CONDITION_KEYS
                raise PolicyFileError(f"правило {self.id}: неизвестное условие {key!r}")
        return True

    def render_reason(self, ctx: Mapping[str, Any]) -> str:
        if not self.reason:
            return ""
        try:
            return self.reason.format(
                tool=str(ctx.get("tool", "")),
                risk=str(ctx.get("risk", "none")),
                action=str(ctx.get("required_action", "")),
                confidence=ctx.get("confidence", 1.0),
            )
        except (KeyError, IndexError, ValueError) as exc:
            raise PolicyFileError(f"правило {self.id}: плохой reason-шаблон: {exc}") from exc


@dataclass(frozen=True, slots=True)
class RuleSet:
    name: str
    rules: tuple[Rule, ...]
    source: str = "builtin"
    raw_sha: str = ""

    def evaluate(self, ctx: Mapping[str, Any]) -> tuple[Rule | None, str]:
        for rule in self.rules:
            if rule.matches(ctx):
                return rule, rule.render_reason(ctx)
        # молча «разрешить, потому что не совпало» — это дыра: новое поле контекста, и политика
        # отпустила всё. Фолбэк-правило обязано быть в наборе; если его нет — отказываем
        return None, "нет правила и нет фолбэка: отказ по умолчанию"

    def as_lock(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sha": self.sha(),
            "rules": {r.id: {"version": r.version, "verdict": r.verdict} for r in self.rules},
        }

    def sha(self) -> str:
        if self.raw_sha:
            return self.raw_sha
        body = orjson.dumps(self.as_lock()["rules"], option=orjson.OPT_SORT_KEYS)
        return hashlib.sha256(body).hexdigest()[:16]


def verdict_rank(verdict: str) -> int:
    """Строгость вердикта: больше = строже. Нужен гейту «ослабили — требуй golden-кейс»."""
    return _VERDICTS.index(verdict)


def default_ruleset() -> RuleSet:
    """Тот же порядок, что был зашит в ``policy.py``, только данными.

    Совпадение не «почти», а точное: ``tests/test_policy_rules.py`` сверяет решения старого
    кода (захардкоженные expectations) с этим набором на всех golden-кейсах.
    """
    rules = (
        Rule(
            "killswitch",
            1,
            {"writes": True, "kill_switch": True},
            "deny",
            "kill switch активен: записи приостановлены владельцем",
            "Стоп — про записи, а не про доступ: чтение живёт и при активном флаге.",
        ),
        Rule(
            "readonly",
            1,
            {"writes": False},
            "allow",
            "read-only",
            "Раньше killswitch: /halt обязан оставлять поиск и чтение заметок рабочими.",
        ),
        Rule(
            "grant-missing",
            1,
            {"writes": True, "permission_missing": True},
            "deny",
            "нет права {action} для этого принципала",
            "RBAC проверяется до всякой оценки риска: гостю не помогает даже low-risk.",
        ),
        Rule(
            "untrusted-write",
            1,
            {"writes": True, "source_trust": ["untrusted"]},
            "confirm",
            "запрос на запись пришёл из внешнего контента",
            "Prompt-injection путь: внешний текст не получает право писать без человека.",
        ),
        Rule(
            "budget-critical",
            1,
            {"writes": True, "budget_ratio_gte": 1.0},
            "deny",
            "личный бюджет на день исчерпан",
            "Право на деградацию: сначала объясни отказ, потом ломай лимит.",
        ),
        Rule(
            "high-risk",
            1,
            {"writes": True, "risk_in": ["high"]},
            "confirm",
            "высокий риск",
            "",
        ),
        Rule(
            "medium-risk",
            1,
            {"writes": True, "risk_in": ["medium"]},
            "confirm",
            "средний риск",
            "",
        ),
        Rule(
            "low-confidence",
            1,
            {"writes": True, "confidence_lt": 0.8},
            "confirm",
            "низкая уверенность извлечения",
            "",
        ),
        Rule(
            "low-risk-auto",
            1,
            {"writes": True, "risk_in": ["low"], "auto_allow_low_risk": True},
            "allow",
            "низкий риск, авто-разрешено",
            "Флаг auto_allow читается условием, а не веткой if — иначе «код и правила» разойдутся.",
        ),
        Rule(
            "system-idempotent",
            1,
            {"writes": True, "source_trust": ["system"], "idempotent": True},
            "allow",
            "идемпотентная системная запись",
            "",
        ),
        Rule(
            "confirm-default",
            1,
            {"writes": True, "always": True},
            "confirm",
            "по умолчанию: подтвердить",
            "deny-by-default на unknown — здесь; фолбэк обязан быть последним.",
        ),
    )
    return RuleSet(name="aegis-default", rules=rules, source="builtin")


def load_ruleset(path: str | Path | None) -> RuleSet | None:
    """Прочитать набор правил из YAML. Нет файла → ``None`` (встроенный набор, принцип 5)."""
    if not path:
        return None
    file = Path(path)
    if not file.is_file():
        return None
    import yaml  # noqa: PLC0415 — опциональный формат: без pyyaml живут настройки-дефолты

    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # noqa: BLE001
        raise PolicyFileError(f"{file.name}: не YAML: {exc}") from exc
    rules: list[Rule] = []
    seen: set[str] = set()
    raw = data.get("rules")
    if not isinstance(raw, list) or not raw:
        raise PolicyFileError(f"{file.name}: rules — непустой список")
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise PolicyFileError(f"{file.name}: правило #{index} — не карта")
        rid = str(item.get("id", ""))
        if not _ID_RE.match(rid):
            raise PolicyFileError(
                f"{file.name}: правило #{index}: id {rid!r} не [a-z0-9._-]{{2,61}}"
            )
        if rid in seen:
            raise PolicyFileError(f"{file.name}: дубль id {rid!r}")
        seen.add(rid)
        verdict = str(item.get("verdict", ""))
        if verdict not in _VERDICTS:
            raise PolicyFileError(f"{file.name}: {rid}: verdict должен быть {','.join(_VERDICTS)}")
        try:
            version = int(item.get("version", 1))
        except (TypeError, ValueError) as exc:
            raise PolicyFileError(f"{file.name}: {rid}: version — целое") from exc
        when_raw = item.get("when") or {}
        if not isinstance(when_raw, Mapping):
            raise PolicyFileError(f"{file.name}: {rid}: when — карта")
        unknown = set(when_raw) - _CONDITION_KEYS
        if unknown:
            raise PolicyFileError(
                f"{file.name}: {rid}: неизвестные условия {sorted(unknown)};"
                f" известно: {sorted(_CONDITION_KEYS)}"
            )
        rules.append(
            Rule(
                id=rid,
                version=version,
                when=dict(when_raw),
                verdict=verdict,
                reason=str(item.get("reason", "")),
                note=str(item.get("note", "")),
            )
        )
    if rules[-1].when.get("always") is not True:
        raise PolicyFileError(f"{file.name}: последнее правило обязано быть always (фолбэк)")
    return RuleSet(
        name=str(data.get("name", file.stem)),
        rules=tuple(rules),
        source=str(file),
        raw_sha=hashlib.sha256(file.read_bytes()).hexdigest()[:16],
    )


def lock_payload(path: str | Path) -> dict[str, Any] | None:
    file = Path(path)
    if not file.is_file():
        return None
    try:
        data = orjson.loads(file.read_bytes())
    except (orjson.JSONDecodeError, OSError):
        return None
    return dict(data) if isinstance(data, Mapping) else None


@dataclass(frozen=True, slots=True)
class RuleChange:
    id: str
    kind: str  # 'added' | 'removed' | 'version' | 'verdict'
    old: str
    new: str

    @property
    def relaxed(self) -> bool:
        return self.kind == "verdict" and verdict_rank(self.new) < verdict_rank(self.old)


def plan_change_vs_lock(
    current: RuleSet, lock: Mapping[str, Any] | None, *, golden_rule_ids: Iterable[str]
) -> list[RuleChange]:
    """Diff набора против lock-файла. Пустой список = «ничего не менялось» (CI-гейт).

    Изменённая версия без смены вердикта — допустима молча (правило могло получить комментарий);
    смена вердикта на более мягкий требует golden-кейс с этим id, иначе CI падает: ровно это и
    превращает «политику поправили на глаз» в измеряемое решение.
    """
    covered = {str(rid) for rid in golden_rule_ids}
    old_rules: Mapping[str, Any] = dict((lock or {}).get("rules") or {})
    changes: list[RuleChange] = []
    for rule in current.rules:
        before = old_rules.get(rule.id)
        if before is None:
            changes.append(RuleChange(rule.id, "added", "", rule.verdict))
            continue
        old_verdict = str(before.get("verdict", ""))
        old_version = int(before.get("version", 0) or 0)
        if old_verdict and verdict_rank(rule.verdict) < verdict_rank(old_verdict):
            changes.append(
                RuleChange(
                    rule.id,
                    "verdict",
                    old_verdict,
                    rule.verdict,
                )
            )
        elif old_version != rule.version:
            changes.append(RuleChange(rule.id, "version", str(old_version), str(rule.version)))
    for rid in old_rules:
        if all(r.id != rid for r in current.rules):
            old_verdict = str(dict(old_rules[rid]).get("verdict", ""))
            changes.append(RuleChange(str(rid), "removed", old_verdict, ""))
    uncovered = [c for c in changes if c.relaxed and c.id not in covered]
    if uncovered:
        raise PolicyFileError(
            "ослабление вердикта без golden-кейса: "
            + ", ".join(f"{c.id} ({c.old}→{c.new})" for c in uncovered)
            + ". Добавьте кейс kind='policy_rule' в evals/policy_rules_v1.jsonl, затем "
            "обновите lock: aegis policy lock"
        )
    return changes


def ruleset_from_records(rows: Sequence[Mapping[str, Any]]) -> RuleSet:
    """Вспомогательное для тестов: собрать набор из dict'ов (без YAML-слоя)."""
    rules = tuple(
        Rule(
            id=str(row["id"]),
            version=int(row.get("version", 1)),
            when=dict(row.get("when") or {}),
            verdict=str(row["verdict"]),
            reason=str(row.get("reason", "")),
        )
        for row in rows
    )
    return RuleSet(name="inline", rules=rules, source="test")
