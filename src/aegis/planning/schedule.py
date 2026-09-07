"""Детерминированный разбор «когда» (шаг 2: напоминания).

Почему парсер, а не «модель сама посчитает время»: напоминание, уехавшее на полтора часа, — это не
опечатка, а просроченная договорённость. У языковой модели нет ни текущего времени с точностью до
секунды, ни права на «примерно в обед». Поэтому модель обязана передать **исходную формулировку**
владельца, а время считаем здесь — по правилам, которые можно прочитать и закрыть тестом.

Что здесь важно:

* все моменты tz-aware в `Settings.timezone`: «завтра в 9» — это 9 утра **у владельца**, а не 9 UTC
  и не 9 на сервере в Docker;
* недостающее время суток дополняется явно и сообщается в `note`: молча додуманное «завтра»
  превращается в «почему ты разбудил меня в шесть»;
* прошлое — ошибка, а не «поставим на ту же минуту вчера»: тик такой строки мгновенно засыпал бы
  владельца просроченными напоминаниями;
* «3.2» — это пункт, а не третье февраля: дату без года признаём только в виде двух пар цифр.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

__all__ = ["When", "WhenNotParsed", "humanize", "parse_when"]

#: Числа словами: владелец пишет в чат, а не в JSON
_WORD_NUMBERS = {
    "один": 1,
    "одна": 1,
    "одну": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "полтора": 1.5,
    "полторы": 1.5,
    "пол": 0.5,
    "полчасика": 0.5,
    "пару": 2,
    "пар": 2,
    "несколько": 3,
}
_UNIT_MINUTES = {
    "мин": 1,
    "минут": 1,
    "час": 60,
    "сут": 1440,
    "дн": 1440,
    "ден": 1440,
    "нед": 10080,
}
_WEEKDAYS = {
    "понедельник": 0,
    "понедельнику": 0,
    "вторник": 1,
    "вторнику": 1,
    "среду": 2,
    "среда": 2,
    "четверг": 3,
    "четвергу": 3,
    "пятницу": 4,
    "пятница": 4,
    "субботу": 5,
    "суббота": 5,
    "воскресенье": 6,
    "воскресень": 6,
}
#: Часть суток вместо часа. Полный список с границами слова: «сегодня» содержит «дня», и
#: подстрокой тут владеть нельзя — иначе каждый «сегодня» становился бы «в 14:00».
_PART_HOURS = {
    "утром": 8,
    "утра": 8,
    "днём": 14,
    "день": 14,
    "вечером": 20,
    "вечер": 20,
    "ночью": 23,
    "ночь": 23,
}
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

_HHMM = r"(?P<h>\d{1,2})[:.](?P<m>\d{2})"
_NUM = r"(?P<n>\d+|" + "|".join(_WORD_NUMBERS) + r")"

_RELATIVE_RE = re.compile(
    rf"через\s+{_NUM}\s*(?P<u>мин\w*|час\w*|сут\w*|дн\w*|ден\w*|нед\w*)", re.I
)
_HALF_HOUR_RE = re.compile(r"через\s+пол\s?часа?", re.I)
#: «через неделю» без числа — частая форма, и «не понял» тут было бы вредительством
_BARE_UNIT_RE = re.compile(
    r"через\s+(?P<u>неделю|недельку|месяц|день|сутки|час|часик|минуту)", re.I
)
_BARE_UNIT_MINUTES = {
    "неделю": 7 * 1440,
    "недельку": 7 * 1440,
    "месяц": 30 * 1440,  # календарный месяц считается 30 днями — и это сказано в note
    "день": 1440,
    "сутки": 1440,
    "час": 60,
    "часик": 60,
    "минуту": 1,
}
_TIME_ONLY_RE = re.compile(rf"(?:в|к|до)\s+{_HHMM}", re.I)
#: «завтра в 9» — час без минут. Отдельная регулярка, потому что «в 9» — не HH:MM, а по-русски
#: это полное и однозначное указание времени
_HOUR_ONLY_RE = re.compile(r"(?:в|к|до)\s+(?P<h>0?[1-9]|1\d|2[0-3])(?![\d:.])", re.I)
_DAY_RE = re.compile(r"(?P<day>послезавтра|завтра|сегодня)", re.I)
_PART_RE = re.compile(
    r"\b(?P<p>" + "|".join(sorted(_PART_HOURS, key=len, reverse=True)) + r")\b", re.I
)
#: дата с годом — однозначные части допустимы; без года — только «03.09» (иначе «пункт 3.2»
#: превращается в третье февраля, а это ровно тот случай, где парсер вреднее отсутствия парсера)
_DATE_FULL_RE = re.compile(r"(?<![\d.])(?P<d>\d{1,2})[.](?P<mo>\d{1,2})[.](?P<y>\d{2,4})(?![\d])")
_DATE_SHORT_RE = re.compile(r"(?<![\d.])(?P<d>\d{2})[.](?P<mo>\d{2})(?![\d.])")
_DATE_WORD_RE = re.compile(r"(?P<d>\d{1,2})\s*(?P<mo>" + "|".join(_MONTHS) + r")\w*", re.I)
_YEAR_ISO_RE = re.compile(
    r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})(?:[ T](?P<h>\d{1,2}):(?P<m>\d{2}))?"
)
_WEEKDAY_RE = re.compile(r"(?:в|по|на)\s+(?P<w>" + "|".join(_WEEKDAYS) + r")", re.I)
_PAST_HINT = re.compile(r"(вчера|прошл|раньш|уже был|утра было)", re.I)
#: «за 15 минут до завтра в 15:00» — lead-фраза. Отдельное правило, а не «пусть модель сама
#  отнимет минуты»: вычитание из уже распознанного момента здесь делает тот же детерминированный
#  код, что и во всём парсере, — и «заранее» перестаёт зависеть от арифметики языковой модели
#: единичное «за час до» без числа — норма разговорного языка, не ошибка; число по умолчанию 1
_LEAD_RE = re.compile(
    rf"за\s+(?P<d>(?:{_NUM}\s*)?(?:мин\w*|час\w*|сут\w*|дн\w*|ден\w*|нед\w*)|пол\s?часа?)"
    r"\s*(?:до|перед(?:\s+тем\s+как)?)\s+(?P<base>\S.*)$",
    re.I,
)
#: «за … до/перед …» на слух есть, а разобрать нечем — молча потерять «заранее» опаснее, чем
#: отказаться: владелец поставит напоминание «на 15:00» вместо «за 10 минут до встречи» и не узнает
_LEAD_ATTEMPT_RE = re.compile(r"\bза\s+\S[^.?!]*?\s(?:до|перед)\s+\S", re.I)

#: «завтра» без времени = утро рабочего дня. Единственный случай, когда время думается за
#: владельца, — и он обязан узнать об этом из note, а не из сообщения в шесть утра.
_DEFAULT_HOUR = 9
_DEFAULT_MINUTE = 0


@dataclass(frozen=True, slots=True)
class When:
    """Распознанный момент: время, что именно сработало и что пришлось дополнить."""

    at: datetime
    matched: str
    note: str = ""


class WhenNotParsed(ValueError):
    """Время не распознано. Молча поставить «на завтра» — значит испортить доверие к функции."""


def parse_when(text: str, *, now: datetime, timezone: str = "UTC") -> When:
    """Разобрать «когда» из человеческой фразы. Вход — ровно то, что написал владелец.

    Порядок правил важнее их числа: «завтра в 10» содержит и день, и время, и их надо применить
    вместе, а не сначала вернуть «завтра 09:00», а потом удивляться.
    """
    raw = (text or "").strip()
    if not raw:
        raise WhenNotParsed("пустое время: скажите «через 20 минут» или «завтра в 9:00»")
    if _PAST_HINT.search(raw):
        raise WhenNotParsed("прошлое время не ставлю: напоминание должно быть впереди")

    zone = _zone(timezone)
    local = now.astimezone(zone)

    # 0) «за N до <база>»: считаем базу обычными правилами, вычитаем — и только потом сверяем с
    #    «не в прошлом ли»: «за 5 минут до» через 4 минуты — не «сейчас», а честный отказ
    lead = _LEAD_RE.search(raw)
    if lead is None and _LEAD_ATTEMPT_RE.search(raw):
        raise WhenNotParsed(
            "слышу «за … до», но не могу посчитать: «за 10 минут до», «за час до», «за 2 дня до»"
        )
    if lead:
        if _LEAD_RE.search(lead.group("base")):
            raise WhenNotParsed("цепочку «за X до за Y до» не разбираю: назовите итоговый момент")
        lead_at = _lead_minutes(lead.group("d"))
        anchor = parse_when(lead.group("base"), now=now, timezone=timezone)
        at = anchor.at - timedelta(minutes=lead_at)
        if at <= local:
            raise WhenNotParsed(
                f"«{lead.group(0).strip()}» приходится на прошедшее: базовый момент слишком близок"
            )
        note = f"считаю заранее: {anchor.matched} минус {lead_at} мин"
        if anchor.note:
            note += f"; {anchor.note}"
        return When(at=at, matched=lead.group(0).strip(), note=note)

    # 1) явная дата: ISO или «дд.мм.гггг» / «дд.мм» / «15 сентября»
    iso = _YEAR_ISO_RE.search(raw)
    if iso:
        day = datetime(int(iso.group("y")), int(iso.group("mo")), int(iso.group("d")), tzinfo=zone)
        return _finish(
            day,
            iso.group(0),
            _hm(iso.group("h"), iso.group("m")) or _time_in(raw, hide=iso.span()),
            local,
        )
    full = _DATE_FULL_RE.search(raw)
    short = None if full else _DATE_SHORT_RE.search(raw)
    if full or short:
        match = full or short
        assert match is not None
        year = match.groupdict().get("y")
        day = _date_or_fail(int(match.group("d")), int(match.group("mo")), year, local)
        return _finish(day, match.group(0), _time_in(raw, hide=match.span()), local)
    word_date = _DATE_WORD_RE.search(raw)
    if word_date:
        month = _month_of(word_date.group("mo"))
        if month:
            day = _date_or_fail(int(word_date.group("d")), month, None, local)
            return _finish(day, word_date.group(0), _time_in(raw, hide=word_date.span()), local)

    # 2) «через N минут/часов/дней»
    relative = _RELATIVE_RE.search(raw)
    if relative:
        minutes = _num(relative.group("n")) * _unit_minutes(relative.group("u"))
        if minutes <= 0:
            raise WhenNotParsed("нулевой интервал — это «сейчас»; напоминание на сейчас не ставлю")
        at = (local + timedelta(minutes=minutes)).replace(second=0, microsecond=0)
        if at <= local:
            raise WhenNotParsed("столь короткое «через» уже ушло бы в прошлое: назовите минуты")
        return When(at=at, matched=relative.group(0).strip())
    bare = _BARE_UNIT_RE.search(raw)
    if bare:
        unit = bare.group("u").lower()
        minutes = _BARE_UNIT_MINUTES[unit]
        at = (local + timedelta(minutes=minutes)).replace(second=0, microsecond=0)
        note = (
            "" if unit != "месяц" else "месяц считаю 30 днями — поправьте, если нужен календарный"
        )
        return When(at=at, matched=bare.group(0).strip(), note=note)
    half = _HALF_HOUR_RE.search(raw)
    if half:
        at = (local + timedelta(minutes=30)).replace(second=0, microsecond=0)
        return When(at=at, matched=half.group(0).strip())

    # 3) день недели: «в пятницу», «в пятницу в 10:00»
    weekday_match = _WEEKDAY_RE.search(raw)
    if weekday_match:
        weekday = _WEEKDAYS.get(weekday_match.group("w").lower())
        if weekday is not None:
            time = _time_in(raw)
            days_ahead = (weekday - local.weekday()) % 7
            base = local + timedelta(days=days_ahead)
            day = _at(base, *(time or _part_time(raw)))
            if day <= local:
                day += timedelta(days=7)
            return When(
                at=day, matched=weekday_match.group(0).strip(), note=_default_note(time, raw, day)
            )

    # 4) «сегодня / завтра / послезавтра» (+ опциональное «в HH:MM»)
    day_match = _DAY_RE.search(raw)
    time = _time_in(raw)
    if day_match:
        offset = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[day_match.group("day").lower()]
        resolved = time or _part_time(raw)
        day = _at(local + timedelta(days=offset), *resolved)
        if day <= local:
            # «сегодня в 08:00», когда уже 15:00, — не «поставь на 8 утра вчера». Тихий перенос
            # на завтра означал бы перепутанную договорённость с опозданием в сутки
            day_word = day_match.group("day")
            raise WhenNotParsed(
                f"«{day_word}» на {day:%H:%M} уже прошло: выберите более поздний момент"
            )
        return When(at=day, matched=day_match.group(0), note=_default_note(time, raw, day))

    # 5) только время: «в 18:30» — сегодня, если успеем, иначе завтра
    if time:
        day = _at(local, time[0], time[1])
        note = ""
        if day <= local:
            day += timedelta(days=1)
            note = "сегодня это время уже прошло — поставил на завтра"
        return When(at=day, matched=_phrase(raw, time), note=note)

    part = _PART_RE.search(raw)  # «вечером», «утром» — без дня недели и без часа
    if part:
        part_hour = _part_time(raw)[0]
        day = _at(local, part_hour, 0)
        note = ""
        if day <= local:
            day += timedelta(days=1)
            note = "ближайшая часть суток уже прошла — поставил на завтра"
        return When(at=day, matched=part.group(0), note=note)

    raise WhenNotParsed(
        f"не понимаю «{raw[:60]}». Умею: «через 20 минут», «завтра в 9», «в 18:30», "
        "«в пятницу в 10:00», «15.09», «2026-09-15T10:00»"
    )


def humanize(at: datetime, *, now: datetime, timezone: str = "UTC") -> str:
    """Короткая человеческая метка: это читает владелец в ответе и в списке напоминаний."""
    zone = _zone(timezone)
    moment = at.astimezone(zone)
    local = now.astimezone(zone)
    delta = moment - local
    # «через …» полезно ровно в горизонте вечера; дальше оно только мешает: «через 25 ч» человек
    # читает как «завтра вечером», а считает секунды вместо того, чтобы посмотреть на часы
    if timedelta(0) <= delta < timedelta(hours=6):
        # округление, а не отбрасывание: цель обнулена до минуты, и «19 мин» вместо «20»
        # выглядело бы как «опоздание», которого нет
        minutes = max(round(delta.total_seconds() / 60), 1)
        if minutes < 60:
            return f"через {max(minutes, 1)} мин"
        hours, rest = divmod(minutes, 60)
        return f"через {hours} ч" + (f" {rest} мин" if rest else "")
    if moment.date() == (local + timedelta(days=1)).date():
        return f"завтра в {moment:%H:%M}"
    if moment.year == local.year:
        return f"{moment:%d.%m} в {moment:%H:%M}"
    return f"{moment:%d.%m.%Y} в {moment:%H:%M}"


# --------------------------------------------------------------------- служебное


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - битый TZ в .env не имеет права валить бота
        return ZoneInfo("UTC")


def _lead_minutes(phrase: str) -> int:
    """«15 минут» / «час» / «полчаса» из lead-части — те же единицы, что у «через N»."""
    p = phrase.strip().lower()
    m = re.match(rf"^(?:{_NUM}\s*)?(?P<u>мин\w*|час\w*|сут\w*|дн\w*|ден\w*|нед\w*)$", p)
    if m:
        number = m.group("n") or "1"
        minutes = int(_num(number) * _unit_minutes(m.group("u")))
    elif p.replace(" ", "") == "полчаса":
        # «полчаса» без числа — устойчивая форма, а не число с единицей; проверять её ДО
        # словесных числительных нельзя: «полтора» тоже начинается на «пол»
        minutes = 30
    else:
        raise WhenNotParsed(f"не понимаю «{phrase.strip()}» как длительность: минуты, часы, дни")
    if minutes <= 0:
        raise WhenNotParsed("нулевое «заранее» — это «в момент»; назовите минуты")
    return minutes


def _num(raw: str) -> float:
    if raw.isdigit():
        return float(raw)
    word = _WORD_NUMBERS.get(raw.lower())
    if word is None:
        raise WhenNotParsed(f"не понимаю число «{raw}»")
    return float(word)


def _at(date: datetime, hour: int, minute: int) -> datetime:
    return date.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _hm(hour: str | None, minute: str | None) -> tuple[int, int] | None:
    return None if hour is None else (int(hour), int(minute or 0))


def _time_in(text: str, *, hide: tuple[int, int] | None = None) -> tuple[int, int] | None:
    """Время суток во фразе; ``hide`` — диапазон уже съеденной даты.

    Без маски «12.09.2026» отдавало бы время «12:09»: дата сама выглядит как HH:MM, и это ровно тот
    случай, когда «умный» разбор хуже честного «не понял».
    """
    pool = text if hide is None else text[: hide[0]] + " " * (hide[1] - hide[0]) + text[hide[1] :]
    match = _TIME_ONLY_RE.search(pool) or re.search(_HHMM, pool) or _HOUR_ONLY_RE.search(pool)
    if not match:
        return None
    hour = int(match.group("h"))
    try:
        minute = int(match.group("m"))
    except (IndexError, ValueError):
        minute = 0
    if hour > 23 or minute > 59:
        raise WhenNotParsed(f"несуществующее время {hour:02d}:{minute:02d}")
    return hour, minute


def _part_time(text: str) -> tuple[int, int]:
    """«вечером»/«утром» без часа — время из части суток; иначе дефолт утра."""
    match = _PART_RE.search(text)
    if match:
        return _PART_HOURS[match.group("p").lower()], 0
    return _DEFAULT_HOUR, _DEFAULT_MINUTE


def _default_note(time: tuple[int, int] | None, raw: str, at: datetime) -> str:
    """Заметка о додуманном времени: пусто, если час или часть суток назвал владелец."""
    if time or _PART_RE.search(raw):
        return ""
    return f"время взято {at:%H:%M} — уточните, если не то"


def _phrase(text: str, time: tuple[int, int]) -> str:
    found = _TIME_ONLY_RE.search(text)
    return found.group(0).strip() if found else f"{time[0]:02d}:{time[1]:02d}"


def _month_of(word: str | None) -> int | None:
    text = (word or "").lower()
    return _MONTHS.get(text[:4]) or _MONTHS.get(text[:3])


def _unit_minutes(unit: str) -> int:
    key = unit.lower()
    for prefix, minutes in _UNIT_MINUTES.items():
        if key.startswith(prefix):
            return minutes
    raise WhenNotParsed(f"не понимаю единицу «{unit}»")


def _date_or_fail(day: int, month: int, year: str | None, local: datetime) -> datetime:
    base_year = local.year if not year else int(f"20{year}" if len(year) == 2 else year)
    try:
        moment = datetime(base_year, month, day, tzinfo=local.tzinfo)
    except ValueError as exc:
        raise WhenNotParsed(f"такой даты не бывает: {exc}") from exc
    if not year and moment < local:
        # «15 декабря» в марте = этот год; если он уже прошёл — следующий, а не «поставил в прошлое»
        moment = moment.replace(year=base_year + 1)
    return moment


def _finish(day: datetime, matched: str, time: tuple[int, int] | None, local: datetime) -> When:
    at = _at(day, *(time or (_DEFAULT_HOUR, _DEFAULT_MINUTE)))
    if at <= local:
        raise WhenNotParsed(f"это уже прошло ({at:%d.%m.%Y %H:%M}): напоминание ставим в будущее")
    return When(
        at=at,
        matched=matched,
        note="" if time else f"время взято {at:%H:%M} — уточните, если не то",
    )
