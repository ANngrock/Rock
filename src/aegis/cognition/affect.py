"""Эмоциональный контур: настроение владельца — состояние в БД, а не догадка на реплику.

Человек не «забывает, что раздражён», между сообщениями. Поэтому: сигнал из каждой реплики
накапливается (valence/frustration), между репликами течёт с экспоненциальным полураспадом
(полдня — и вчерашняя злость не управляет тоном ответа; управляет — было бы неуважением).

Локальный детектор — эвристика по словам/регистрам, работает без LLM (принцип 5: деградация,
а не отказ). LLM-вердикт, когда есть модель, уточняет эвристику, а не заменяет: молчаливый
фаст-путь всегда остаётся.

Настроение влияет на: подбор эмодзи/стикера и одну строку контекста для модели («не читай
мораль — человек на взводе»). На решение и права — ни на йоту: это тон, а не политика.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

__all__ = ["Affect", "SqlAffectStore", "apply_signal", "detect_local", "mood_emoji", "mood_name"]

#: полураспад накопленного настроения: 6 часов — «вечером за день уже остыли»
HALFLIFE = timedelta(hours=6)

_JOY = (
    "спасибо",
    "благодар",
    "отлично",
    "супер",
    "класс",
    "ура",
    "здорово",
    "люблю",
    "🎉",
    "😄",
    "😀",
)
_ANGER = (
    "бесит",
    "раздража",
    "достал",
    "сколько раз",
    "опять",
    "снова косяк",
    "терпеть не могу",
    "😡",
    "🤬",
)
_SAD = ("устал", "вымотан", "плохо", "грустно", "тяжело", "не могу больше", "😞", "😔")
_ANXIOUS = ("срочно", "скорее", "аврал", "горим", "дедлайн", "до вечера", "нужно ещё вчера", "⏰")

_UPSHOUT = 0.7  # доля верхнего регистра в коротком слове-крике
_SHORT = 60


@dataclass(frozen=True, slots=True)
class Affect:
    valence: float = 0.0  # -1..1 — «как вообще всё»
    arousal: float = 0.3  # 0..1 — «насколько заряжен»
    frustration: float = 0.0  # 0..1 — накопленное «опять»
    mood: str = "neutral"
    turns: int = 0

    def decayed_to(self, now: datetime, since: datetime) -> Affect:
        hours = max(0.0, (now - since).total_seconds() / 3600.0)
        k = math.exp(-math.log(2) * hours / (HALFLIFE.total_seconds() / 3600.0))
        return replace(
            self,
            valence=self.valence * k,
            arousal=0.3 + (self.arousal - 0.3) * k,
            frustration=self.frustration * k,
        )


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def detect_local(msg: str) -> dict[str, float]:
    """Дельты настроения из одной реплики — без модели. Тишина = (0,0,0)."""
    low = msg.lower()
    hits = {
        "joy": sum(1 for w in _JOY if w in low),
        "anger": sum(1 for w in _ANGER if w in low),
        "sad": sum(1 for w in _SAD if w in low),
        "anxious": sum(1 for w in _ANXIOUS if w in low),
    }
    delta = {"valence": 0.0, "arousal": 0.0, "frustration": 0.0}
    if hits["joy"]:
        delta["valence"] += min(0.5, 0.25 * hits["joy"])
        delta["arousal"] += 0.15 * min(2, hits["joy"])
    if hits["anger"]:
        delta["frustration"] += min(0.6, 0.3 * hits["anger"])
        delta["valence"] -= min(0.5, 0.25 * hits["anger"])
        delta["arousal"] += 0.2
    if hits["sad"]:
        delta["valence"] -= min(0.4, 0.2 * hits["sad"])
    if hits["anxious"]:
        delta["arousal"] += min(0.4, 0.2 * hits["anxious"])
    if "!!!" in msg:
        delta["arousal"] += 0.2
    if low.count("...") >= 1:
        delta["valence"] -= 0.1
    letters = [c for c in msg if c.isalpha()]
    if letters and len(msg) <= 40:
        caps = sum(1 for c in letters if c.isupper()) / len(letters)
        if caps > _UPSHOUT and len(letters) >= 3:  # «ГДЕ ОТЧЁТ» — крик, а не аббревиатура
            delta["arousal"] += 0.25
            delta["frustration"] += 0.2
    return {k: _clamp(v, -1.0, 1.0) for k, v in delta.items()}


def mood_name(affect: Affect) -> str:
    if affect.frustration > 0.4 and affect.valence < 0.1:
        return "irritated"
    if affect.valence > 0.35:
        return "joy"
    if affect.valence < -0.3 and affect.frustration <= 0.45:
        return "sad"
    if affect.arousal > 0.65:
        return "anxious"
    return "neutral"


_MOOD_EMOJI = {"joy": "😄", "irritated": "😤", "sad": "🫂", "anxious": "⏳", "neutral": ""}


def mood_emoji(mood: str) -> str:
    return _MOOD_EMOJI.get(mood, "")


#: сколько официоза убивает эмодзи: выше — письмо, а не переписка
_FORMAL_CUTOFF = 0.6


def emoji_for_reply(affect: Affect, formality: float) -> str:
    """Один эмодзи в тон — или ничего. Смайликами через строчку человек не разбрасывается."""
    if formality >= _FORMAL_CUTOFF:
        return ""
    emoji = mood_emoji(affect.mood)
    return "" if emoji and emoji in ("😄",) and affect.valence < 0.25 else emoji


def apply_signal(affect: Affect, delta: dict[str, float], *, now: datetime | None = None) -> Affect:
    """Сигнал реплики поверх (уже протухшего) состояния; молчание (все нули) не двигает ничего."""
    if not any(abs(v) > 1e-9 for v in delta.values()):
        return replace(affect, mood=mood_name(affect), turns=affect.turns + 1)
    merged = Affect(
        valence=_clamp(affect.valence + delta.get("valence", 0.0) * 0.6),
        arousal=_clamp(affect.arousal + delta.get("arousal", 0.0) * 0.6, 0.0, 1.0),
        frustration=_clamp(affect.frustration + delta.get("frustration", 0.0) * 0.7, 0.0, 1.0),
        mood=affect.mood,
        turns=affect.turns + 1,
    )
    return replace(merged, mood=mood_name(merged))


def affect_hint(affect: Affect) -> str:
    """Строка в <cognition>-блок модели: тон, не права."""
    bits: list[str] = []
    if affect.frustration > 0.5:
        bits.append("владелец раздражён (накоплено) — без морали и лишних объяснений, дело вперёд")
    elif affect.valence > 0.4:
        bits.append("у владельца подъём — можно коротко разделить")
    elif affect.valence < -0.3:
        bits.append("владелец на спаде — теплее, короче, без канцелярита")
    if affect.arousal > 0.7 and not bits:
        bits.append("высокий заряд: человек торопится, сначала ответ, потом детали")
    return "; ".join(bits)


_UPSERT = """
    INSERT INTO cognition.affect_state
        (owner_id, valence, arousal, frustration, mood, turns, updated_at)
    VALUES (:o, :v, :a, :f, :m, 1, now())
    ON CONFLICT (owner_id) DO UPDATE SET
        valence = EXCLUDED.valence, arousal = EXCLUDED.arousal,
        frustration = EXCLUDED.frustration, mood = EXCLUDED.mood,
        turns = cognition.affect_state.turns + 1, updated_at = now()
"""
_SELECT = """
    SELECT valence, arousal, frustration, mood, turns, updated_at
      FROM cognition.affect_state
     WHERE owner_id = :o
"""


class SqlAffectStore:
    """Состояние на household; чтение-изменение-запись в одной сессии (гонка двух чатов — редкость,
    потеря одного сигнала — не трагедия: контур — настроение, не счётчик денег)."""

    def __init__(self, *, session_factory: Any = None) -> None:
        self._sm = session_factory

    def _session(self) -> Any:
        from aegis.platform.db import session

        return self._sm() if self._sm is not None else session()

    async def current(self, owner_id: int) -> tuple[Affect, datetime | None]:
        async with self._session() as s:
            row = (await s.execute(text(_SELECT), {"o": int(owner_id)})).mappings().first()
        if row is None:
            return Affect(), None
        return (
            Affect(
                valence=float(row["valence"]),
                arousal=float(row["arousal"]),
                frustration=float(row["frustration"]),
                mood=str(row["mood"]),
                turns=int(row["turns"]),
            ),
            row["updated_at"],
        )

    async def signal(self, owner_id: int, delta: dict[str, float]) -> Affect:
        now = datetime.now(UTC)
        current, since = await self.current(owner_id)
        base = current.decayed_to(now, since) if since is not None else current
        merged = apply_signal(base, delta, now=now)
        async with self._session() as s:
            await s.execute(
                text(_UPSERT),
                {
                    "o": int(owner_id),
                    "v": merged.valence,
                    "a": merged.arousal,
                    "f": merged.frustration,
                    "m": merged.mood,
                },
            )
            await s.commit()
        return merged
