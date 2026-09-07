"""Воронка: входящая реплика → understanding → augmented query; ответ → outbound-сборка.

Идея «думать как человек» переведена честно, без магии: человек слушает (нормализует),
узнаёт регистр (письмо это или «го»), вспоминает словарь, считывает состояние собеседника,
отвечает структурированно и подбирает тон. Каждый шаг — чистая функция над dataclass: прогоняется
тестом без сети, пишется в трассу, падает деградацией (один шаг без данных не отменяет ход), а не
исключением. Всё, что шагам нужно от мира (лексикон, affect), передаётся уже готовым —
воронка не знает про БД намеренно: ввод/вывод строки, побочные эффекты снаружи.

Что воронка НЕ делает: не решает права (policy engine), не цензурирует (DLP), не переписывает
содержание ответа. Форматирование и тон — косметика поверх смысла; смысл портить права нет.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from aegis.cognition.affect import Affect
from aegis.cognition.lexicon import LexEntry, hits_in, prompt_block

__all__ = [
    "FunnelState",
    "Register",
    "build_inbound",
    "compose_outbound",
    "detect_register",
    "normalize_text",
]

_WS = re.compile(r"[ \t]+")
_EDGE = re.compile(r"[ \t]*\n[ \t]*")
_MULTINL = re.compile(r"\n{3,}")
_EMOJI_RUN = re.compile(r"[\U0001F000-\U0001FAFF☀-➿]+")

_FORMAL = (
    "прошу вас",
    "прошу тебя",
    "уважа",
    "согласно",
    "касательно",
    "буду благодарен",
    "буду благодарна",
    "не могли бы",
    "не мог бы",
    "настоятел",
    "уведомля",
    "с уважением",
    "довожу до",
    "во исполнени",
)
_PATRONYMIC = re.compile(r"\b[А-ЯЁ][а-яё]{2,}(?:вич|овна|ична|евна)\b")
_SLANG = (
    "го ",
    " го",
    "имба",
    "краш",
    "норм",
    "забей",
    "забить",
    "рофл",
    "лол",
    "ага",
    "угу",
    "ок ",
    "окей",
    "кэш",
    "залип",
    "чилл",
    "вайб",
    "красава",
    "тащит",
    "подго",
    "подгон",
    "срч",
    "спс",
    "пжлста",
    "пжл",
    "прив",
    "здорово",
    "чо",
    "чё",
    "них",
    "ну",
    "бли",
    "блин",
)
_URGENT = (
    "срочно",
    "как можно скорее",
    "немедленно",
    "до вечера",
    "до обеда",
    "нужно вчера",
    "горим",
    "⏰",
    "‼",
)


@dataclass(frozen=True, slots=True)
class Register:
    """Что воронка поняла про «как говорят», а не «что говорят»."""

    formality: float = 0.5
    slangy: bool = False
    urgent: bool = False
    shouty: bool = False
    emoji_count: int = 0

    @property
    def label(self) -> str:
        if self.formality >= 0.75:
            return "официальное письмо"
        if self.formality <= 0.3:
            return "свойский тон, жаргон допустим"
        return "обычная переписка"


@dataclass(slots=True)
class FunnelState:
    raw: str
    text: str
    register: Register
    lex_hits: list[LexEntry] = field(default_factory=list)
    voice: bool = False
    notes: list[str] = field(default_factory=list)
    augmented: str = ""
    mood: str = "neutral"
    emoji: str = ""


def normalize_text(msg: str) -> str:
    out = _WS.sub(" ", (msg or "").replace("\r", "\n"))
    out = _EDGE.sub("\n", out)  # «слово \n\t слово» — для человека это одна строка
    out = _MULTINL.sub("\n\n", out)
    return out.strip()


def detect_register(text: str) -> Register:
    low = text.lower()
    score = 0.5
    for marker in _FORMAL:
        if marker in low:
            score += 0.15
    if _PATRONYMIC.search(text):
        score += 0.2
    slang_hits = sum(1 for w in _SLANG if w in low)
    if slang_hits:
        score -= 0.12 * slang_hits
    if text and not text[-1].isalpha():
        score -= 0.05  # без точки в конце — переписка, не докладная
    emoji_count = len(_EMOJI_RUN.findall(text))
    score -= 0.08 * min(emoji_count, 3)
    letters = [c for c in text if c.isalpha()]
    shouty = (
        bool(letters)
        and len(letters) <= 40
        and sum(c.isupper() for c in letters) / max(1, len(letters)) > 0.7
    )  # noqa: PLR2004
    urgent = any(m in low for m in _URGENT) or "!!!" in text
    return Register(
        formality=max(0.0, min(1.0, score)),
        slangy=slang_hits >= 2,
        urgent=urgent,
        shouty=shouty,
        emoji_count=emoji_count,
    )


def build_inbound(
    text: str,
    *,
    lexicon: list[LexEntry],
    affect: Affect | None,
    voice: bool = False,
    extra_notes: list[str] | None = None,
) -> FunnelState:
    """Полная входная воронка. Никаких вызовов наружу: всё, что нужно, — на входе."""
    norm = normalize_text(text)
    reg = detect_register(norm)
    hits = hits_in(lexicon, norm)
    state = FunnelState(raw=text, text=norm, register=reg, lex_hits=hits, voice=voice)
    notes: list[str] = []
    if voice:
        notes.append(
            "это голосовое (распознано текстом): имена и числа могли искажаться — при важном"
            " расхождении уточни одним вопросом"
        )
    if reg.formality >= 0.75:
        notes.append("регистрация: письмо — отвечай так же: без эмодзи, полно, по абзацам")
    elif reg.formality <= 0.3:
        notes.append("регистрация: своя переписка — короткий прямой ответ, не канцелярия")
    if reg.urgent:
        notes.append("человек торопится: сначала ответ, потом пояснения")
    if affect is not None and affect.mood != "neutral":
        bits = {
            "irritated": "раздражён",
            "joy": "на подъёме",
            "sad": "на спаде",
            "anxious": "на взводе",
        }
        mood = bits.get(affect.mood, affect.mood)
        notes.append(f"настроение собеседника: {mood} — тон подбирай, суть не меняй")
    if extra_notes:
        notes.extend(extra_notes)
    state.notes = notes
    state.augmented = _augment(norm, state)
    return state


def _augment(norm: str, state: FunnelState) -> str:
    block: list[str] = []
    block.extend(state.notes)
    lex = prompt_block(state.lex_hits)
    if lex:
        block.append(lex)
    if not block:
        return norm
    return f"{norm}\n\n<cognition>\n" + "\n".join(block) + "\n</cognition>"


def compose_outbound(
    reply_text: str,
    state: FunnelState,
    *,
    mood_emoji: str = "",
    allow_emoji: bool = True,
) -> str:
    """Выходная сборка: структура + нота тона. Только косметика: ни слова от себя не добавляем,
    ни слова не выкидываем — правдивость ответа отвечают модель и верификатор, не воронка."""
    text = reply_text.rstrip()
    if not text:
        return text
    # «выглядело структурно»: у длинного плоского ответа — заголовок из первой строки
    lines = text.split("\n")
    if len(text) > 700 and "<b>" not in text and len(lines) >= 4 and 0 < len(lines[0]) <= 80:  # noqa: PLR2004
        if "<" not in lines[0]:
            text = f"<b>{lines[0]}</b>\n" + "\n".join(lines[1:])
    if allow_emoji and mood_emoji and state.register.formality < 0.6 and not state.register.urgent:
        already = text.endswith(mood_emoji) or text.rstrip().endswith(f"{mood_emoji}")
        if not already and "<code>" not in text[-40:]:
            text = f"{text} {mood_emoji}"
    return text


def sticker_gate(
    state: FunnelState, affect: Affect | None, *, allow_when_neutral: bool = False
) -> bool:
    """Стикер уместен не всегда: письмо с наклейкой — это не «дорого», это невнимательность."""
    if state.register.formality >= 0.6:
        return False
    if affect is None:
        return allow_when_neutral
    return affect.mood != "neutral" or allow_when_neutral


def extract_request(text: str) -> dict[str, Any]:
    """Мелкие решения по форме запроса, которые иначе модель приняла бы вслепую."""
    low = text.lower()
    return {
        "question": "?" in text,
        "wants_voice": "голосом" in low or "наговори" in low or "аудио" in low,
        "wants_brief": any(w in low for w in ("короче", "коротко", "в двух словах", "кратко")),
    }
