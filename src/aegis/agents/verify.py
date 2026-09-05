"""Verifier: ответ сверяется с источниками до того, как его увидит владелец (шаг 2).

Зачем это отдельный слой, а не строчка в системном промпте: «проверь числа по источникам» —
обещание модели, а обещание проверкой не является. Здесь сверка состоит из двух независимых шагов,
и первый вообще не требует LLM:

1. **детерминированная сверка** — все нетривиальные числа и даты ответа должны читаться в
   результатах инструментов (или в самом вопросе владельца). То, чего там нет, помечается как
   непроверенное — ровно тот класс ошибки, из-за которого «умный» ответ на полправды стоит дороже
   честного «не знаю»;
2. **судья в чистом контексте** — модель видит только вопрос, ответ и источники: без истории
   разговора, без системного промпта, без инструментов. Командовать ей нечем, а «додумать» то, чего
   в источниках нет, ей не позволяет схема ответа.

Вердикт ничего не запрещает и не цензурирует: он добавляет владельцу строку ⚠️ и запись в журнал.
«Ответ не подтверждён источниками» — факт, который владелец должен увидеть, а не то, от чего можно
молча избавиться.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field

from aegis.platform.config import Settings, settings
from aegis.platform.gateway.client import ModelGateway
from aegis.platform.prompts import PromptNotFound
from aegis.platform.prompts import load as load_prompt

__all__ = ["Verdict", "Verifier", "VerifyJudgement", "extract_claims", "find_unverified"]

log = structlog.get_logger(__name__)

#: Сколько утверждений сверять за раз. Больше — уже не «проверка ответа», а разбор простыни, и
#: стоимость проверки начинает догонять стоимость ответа.
_MAX_CLAIMS = 12
#: Числа не длиннее этих символов без дробной части — служебные («2 варианта», «1.»): сверять нечего
_TRIVIAL_DIGITS = 1
_MONEY_HINT = re.compile(
    r"(курс|сумм|бюджет|платеж|платёж|стоим|цена|долл|евро|рубл|грн|₴|\$|€|₽)", re.I
)

#: число с разделителями разрядов и/или дробной частью. Хвост `(?![.,]\d)` отделяет «следующее
#: число» от «точки в конце предложения»: без него «было 42.9.» не извлекалось бы вовсе.
_NUMBER_RE = re.compile(
    r"(?<![\d.,])\d{1,3}(?:[\s\u00a0\u202f]\d{3})*(?:[.,]\d{1,6})?(?!\d)(?![.,]\d)"
)
#: дата с годом: дд.мм.гггг, дд-мм-гггг — здесь день и месяц могут быть однозначными
_DATE_RE = re.compile(r"(?<![\d.])(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})(?!\d)")
#: дата без года — только если обе части по две цифры: «3.2» (пункт, версия, «3.2 литра») датой не
#: является, и пускать её в сверку значит плодить ложные «не подтверждено»
_DATE_SHORT_RE = re.compile(r"(?<![\d.])(\d{2})[./-](\d{2})(?![\d.])")
_DATE_ISO_RE = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d-])")
#: «3 сентября», «1 окт. 2026»
_DATE_WORD_RE = re.compile(r"(?<![\d])(\d{1,2})\s*([а-яё]{3,8})\.?(?:\s*(\d{4}))?", re.I)
_MONTHS = {
    "янв": 1,
    "февр": 2,
    "мар": 3,
    "апр": 4,
    "мая": 5,
    "май": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сент": 9,
    "окт": 10,
    "нояб": 11,
    "дек": 12,
}
#: нецифровой заполнитель на месте даты: числа внутри даты — не числа (иначе «03.09.2026»
#: дало бы ложную находку «09.2026»)
_GAP = "\x00"


class VerifyJudgement(BaseModel):
    """Схема вердикта судьи — контракт промпта типом, а не абзацем текста.

    ``severity`` отдельным полем потому, что «расхождение в формулировке» и «расхождение в сумме» —
    разные события: первое владелец переживёт, второе решается деньгами.
    """

    consistent: bool
    severity: Literal["none", "minor", "critical"] = "none"
    unsupported: list[str] = Field(default_factory=list)
    corrections: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Verdict:
    """Итог сверки. ``ok`` — «отвечаем как есть»; проблемы при этом могут быть."""

    ok: bool
    severity: Literal["none", "minor", "critical", "unavailable"] = "none"
    checked: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    judged: bool = False
    #: "judged" | "deterministic" | "unavailable"
    mode: str = "deterministic"
    cost_usd: float = 0.0
    latency_ms: int = 0
    note: str = ""
    #: чем именно судили (id/version/sha промпта судьи): вердикт без критерия — то же самое, что
    #  ответ без источника; по этим трём полям через год видно, не сменились ли правила сравнения
    prompt_ids: tuple[Mapping[str, str], ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def notice(self) -> str | None:
        """Строка для владельца. Её добавляем мы, а не модель: пересказ — её право, а обязанность
        сказать «не подтверждается источниками» — наша."""
        if self.ok and not self.problems:
            return None
        head = "ответ не подтверждён источниками" if not self.ok else "проверка ответа"
        tail = "; ".join(self.problems[:3])
        if len(self.problems) > 3:
            tail += f"; и ещё {len(self.problems) - 3}"
        extra = "" if self.judged else " (судья недоступен — только сверка чисел)"
        return f"{head}: {tail}{extra}"[:400]


def _norm_number(raw: str) -> str:
    """Канонический вид числа: «1 234,56», «1234.560» и «1234.56» — это одно и то же.

    Хвостовые нули дробной части снимаем: источник пишет «42.900», модель отвечает «42,9» —
    назвать это расхождением означало бы научить владельца не читать предупреждения.
    """
    digits = re.sub(r"[\s\u00a0\u202f]", "", raw)
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", digits):  # точка как разделитель разрядов
        digits = digits.replace(".", "")
    digits = digits.replace(",", ".")
    if "." in digits:
        digits = digits.rstrip("0").rstrip(".")
    return digits


def _norm_date(day: str, month: str, year: str) -> str | None:
    try:
        d, m = int(day), int(month)
    except (TypeError, ValueError):
        return None
    if not (1 <= d <= 31 and 1 <= m <= 12):
        return None
    y = (year or "").strip()
    if len(y) == 2:
        y = f"20{y}"
    return f"{d:02d}.{m:02d}.{y or 'гггг'}"


def _display(claim: str) -> str:
    """Как показывать владельцу нормализованную дату: `05.03.гггг` → `05.03`.

    Канон нужен для сравнения, а не для чтения: «в ответе есть «05.03.гггг», чего нет в
    источниках» — строчка, после которой предупреждениям перестают верить.
    """
    return re.sub(r"\.гггг$", "", claim)


_SEVERITY_ORDER = {"none": 0, "minor": 1, "critical": 2, "unavailable": 1}


def _worse(
    left: Literal["none", "minor", "critical", "unavailable"],
    right: Literal["none", "minor", "critical"],
) -> Literal["none", "minor", "critical", "unavailable"]:
    """Более тяжёлая из двух оценок: верификатор не имеет права «успокоить» находку."""
    return right if _SEVERITY_ORDER[right] > _SEVERITY_ORDER[left] else left


def _month_of(word: str | None) -> int | None:
    """Месяц по началу слова: «3 сентября» и «1 окт.» должны читаться одинаково.

    Один фиксированный срез не годится: «сент» — четыре буквы, «окт» — три.
    """
    text = (word or "").lower()
    return _MONTHS.get(text[:4]) or _MONTHS.get(text[:3])


def _date_spans(text: str) -> list[tuple[int, int, str]]:
    """Все даты текста как (начало, конец, канон) — по возрастанию, без пересечений."""
    found: list[tuple[int, int, str]] = []
    for match in _DATE_ISO_RE.finditer(text):
        canon = _norm_date(match.group(3), match.group(2), match.group(1))
        if canon:
            found.append((match.start(), match.end(), canon))
    for match in _DATE_RE.finditer(text):
        if any(start <= match.start() < end for start, end, _ in found):
            continue  # ISO уже покрыт: «2026-09-03» не должен родить ещё и «2026.09»
        canon = _norm_date(match.group(1), match.group(2), match.group(3))
        if canon:
            found.append((match.start(), match.end(), canon))
    for match in _DATE_SHORT_RE.finditer(text):
        if any(start <= match.start() < end for start, end, _ in found):
            continue
        canon = _norm_date(match.group(1), match.group(2), "")
        if canon:
            found.append((match.start(), match.end(), canon))
    for match in _DATE_WORD_RE.finditer(text):
        month = _month_of(match.group(2))
        if month is None or any(start <= match.start() < end for start, end, _ in found):
            continue
        canon = _norm_date(match.group(1), str(month), match.group(3) or "")
        if canon:
            found.append((match.start(), match.end(), canon))
    return sorted(found)


def _hide_dates(text: str) -> str:
    """Даты → небуквенно-цифровой заполнитель, чтобы числовые регулярки не резали дату на числа."""
    out: list[str] = []
    pos = 0
    for start, end, _canon in _date_spans(text):
        out.append(text[pos:start])
        out.append(_GAP * (end - start))
        pos = end
    out.append(text[pos:])
    return "".join(out)


def extract_claims(text: str) -> tuple[str, ...]:
    """Числа и даты, которые обязаны быть подтверждены источником.

    Возвращает нормализованные строки («1234.56», «03.09.2026»): сравнение по канону, иначе «43,18»
    в ответе и «43.18» в источнике считались бы расхождением — а это ровно тот шум, из-за которого
    проверку перестают читать.
    """
    out: list[str] = []
    seen: set[str] = set()
    for _start, _end, canon in _date_spans(text):
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
    for match in _NUMBER_RE.finditer(_hide_dates(text)):
        digits = _norm_number(match.group(0))
        whole, _, frac = digits.partition(".")
        if len(whole) <= _TRIVIAL_DIGITS and not frac:
            continue
        if "." not in digits and len(digits) == 4 and digits.startswith("20"):
            continue  # одиночный год без контекста — метка времени текста, а не утверждение
        if digits in seen:
            continue
        seen.add(digits)
        out.append(digits)
    return tuple(out)


_DATE_CANON_RE = re.compile(r"(\d{2}\.\d{2})\.(гггг|\d{4})")


def find_unverified(claims: Sequence[str], haystacks: Sequence[str]) -> list[str]:
    """Что из ``claims`` не находится ни в одном источнике (сравнение по нормализованному виду)."""
    pool = " | ".join(_normalize(item) for item in haystacks)
    # даты источника — отдельным множеством: «не повторили год» и «перепутали год» для поиска
    # подстроки выглядят одинаково, а значат ровно противоположное
    pool_dates = set(_DATE_CANON_RE.findall(pool))
    missing: list[str] = []
    for claim in claims:
        if claim in pool:
            continue
        match = _DATE_CANON_RE.fullmatch(claim)
        if match:
            daymonth, year = match.groups()
            # год могли не повторить — с любой из сторон; требовать его нельзя: источник имеет
            # право написать «3 сентября», а ответ — «03.09.2026»
            if any(dm == daymonth and y == "гггг" for dm, y in pool_dates):
                continue
            if year == "гггг" and any(dm == daymonth for dm, _ in pool_dates):
                continue
        missing.append(claim)
    return missing


def _normalize(text: str) -> str:
    """Текст источника — в ту же нормализацию, что и claims: сначала даты, потом числа."""
    spans = _date_spans(text)
    out: list[str] = []
    pos = 0
    for start, end, canon in spans:
        out.append(_NUMBER_RE.sub(lambda m: _norm_number(m.group(0)), text[pos:start]))
        out.append(f" {_GAP}{canon}{_GAP} ")
        pos = end
    out.append(_NUMBER_RE.sub(lambda m: _norm_number(m.group(0)), text[pos:]))
    return "".join(out).replace(_GAP, " ")


class Verifier:
    """Сверка ответа с источниками: сначала числа, потом — судья в чистом контексте."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        cfg: Settings | None = None,
        prompt_id: str = "verify/judge",
    ) -> None:
        self.gateway = gateway
        self.cfg = cfg or settings()
        self.prompt_id = prompt_id

    # --------------------------------------------------------- «нужно ли проверять»

    def should_verify(self, *, answer: str, sources: Sequence[str]) -> bool:
        """Проверяем, когда есть что сверять и чем. Иначе — тише воды: платная сверка болтовни
        выглядела бы заботой, а была бы расходом."""
        if not self.cfg.verify_enabled or not sources:
            return False
        if len(answer) < self.cfg.verify_min_answer_chars:
            return False
        if extract_claims(answer):
            return True
        # «всегда» = сверять и ответы без чисел: выдуманный качественный факт («НБУ отменил
        # публикацию») числами не ловится, а стоит ровно столько же
        return self.cfg.verify_always

    # --------------------------------------------------------- сама сверка

    async def verify(
        self,
        *,
        question: str,
        answer: str,
        sources: Sequence[str],
        trace_id: str = "",
        turn_no: int = 0,
        owner_id: int = 0,
    ) -> Verdict:
        """Ни один отказ здесь не имеет права отменить ответ: «не смог проверить» честнее, чем
        выставленное по умолчанию «всё хорошо»."""
        started = time.perf_counter()
        all_claims = extract_claims(answer)
        claims = all_claims[:_MAX_CLAIMS]
        skipped = max(0, len(all_claims) - len(claims))
        haystacks = [*sources, question]
        missing = find_unverified(claims, haystacks)
        problems = [
            f"в ответе есть «{_display(item)}», чего нет в источниках" for item in missing[:6]
        ]
        if skipped:
            problems.append(f"и ещё {skipped} чисел не проверялись (лимит сверки)")
        severity: Literal["none", "minor", "critical", "unavailable"] = (
            "critical"
            if missing and _MONEY_HINT.search(answer)
            else ("minor" if missing else "none")
        )
        payload: dict[str, Any] = {
            "question": question[:4000],
            "answer": answer[:8000],
            "sources": [item[:3000] for item in list(sources)[: self.cfg.verify_max_sources]],
            "claims": list(claims),
            "missing": missing,
        }
        judged = False
        note = ""
        prompt_meta: tuple[Mapping[str, str], ...] = self._prompt_ref()
        # судья зовётся на любую сверку: «числа совпали» не значит, что выдумано меньше
        judgement = await self._judge(question=question, answer=answer, sources=sources)
        if judgement is None:
            note = "судья недоступен"
            severity = severity if missing else "unavailable"
        else:
            judged = True
            extra = [item for item in judgement.unsupported if item][:6]
            fixes = [f"уточнение: {item}" for item in judgement.corrections[:3] if item]
            problems = list(dict.fromkeys([*problems, *extra, *fixes]))[:8]
            if judgement.consistent and not missing:
                # расхождение чисел было ложным: судья подтвердил, год в источнике опущен
                problems, severity = [], "none"
            else:
                severity = _worse(severity, judgement.severity)
        latency_ms = int((time.perf_counter() - started) * 1000)
        ok = not problems
        log.info(
            "verify.done",
            trace_id=trace_id,
            ok=ok,
            severity=severity,
            claims=len(claims),
            missing=len(missing),
            judged=judged,
        )
        return Verdict(
            ok=ok,
            severity=severity,
            checked=claims,
            problems=tuple(problems),
            judged=judged,
            mode="judged" if judged else ("unavailable" if note else "deterministic"),
            latency_ms=latency_ms,
            note=note,
            prompt_ids=prompt_meta,
            payload=payload,
        )

    def _prompt_ref(self) -> tuple[Mapping[str, str], ...]:
        """Ссылка на промпт судьи для записи журнала (пусто — если файла нет: вердикт это учтёт)."""
        try:
            return (load_prompt(self.prompt_id).as_record(),)
        except PromptNotFound:
            return ()

    async def _judge(
        self, *, question: str, answer: str, sources: Sequence[str]
    ) -> VerifyJudgement | None:
        """Один вызов с чистым контекстом: ни истории, ни инструментов, ни промпта хода."""
        try:
            prompt = load_prompt(self.prompt_id)
        except PromptNotFound as exc:
            log.warning("verify.prompt_missing", err=str(exc)[:200])
            return None
        joined = "\n\n".join(item[:3000] for item in list(sources)[: self.cfg.verify_max_sources])
        body = prompt.render(
            question=question[:4000], answer=answer[:8000], sources=joined or "(источников нет)"
        )
        messages = [
            {"role": "system", "content": body},
            {"role": "user", "content": "Сверь ответ с источниками и верни JSON по схеме."},
        ]
        try:
            return await self.gateway.chat_json(
                prompt.model_role,  # type: ignore[arg-type]
                messages,
                VerifyJudgement,
                thinking=prompt.thinking,
                temperature=prompt.temperature,
            )
        except Exception as exc:  # noqa: BLE001 - отказ судьи не имеет права ломать ответ
            log.warning("verify.judge_failed", err=repr(exc)[:200])
            return None
