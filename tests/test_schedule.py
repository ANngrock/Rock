"""Разбор «когда» для напоминаний.

Время в напоминаниях — единственное место шага 2, где цена ошибки видна не в логе, а в испорченном
дне владельца. Поэтому здесь проверяются именно границы правил: что парсер считает, что он
отвергает и о чём обязан предупредить. Все тесты передают ``now`` — привязки к часам процесса нет.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from aegis.planning.schedule import WhenNotParsed, humanize, parse_when

#: суббота, 15:00 по Москве
NOW = datetime(2026, 9, 5, 15, 0, tzinfo=ZoneInfo("Europe/Moscow"))
MSK = "Europe/Moscow"


def when(text: str, *, now: datetime = NOW, tz: str = MSK) -> datetime:
    return parse_when(text, now=now, timezone=tz).at


def note(text: str, *, now: datetime = NOW, tz: str = MSK) -> str:
    return parse_when(text, now=now, timezone=tz).note


# ----------------------------------------------------------- относительное --


def test_relative_is_counted_from_owner_now() -> None:
    assert when("через 20 минут") == NOW + timedelta(minutes=20)
    assert when("через 2 часа") == NOW + timedelta(hours=2)
    assert when("через 90 минут") == NOW + timedelta(hours=1, minutes=30)
    assert when("через 3 дня") == NOW + timedelta(days=3)
    assert when("через неделю") == NOW + timedelta(weeks=1)


def test_words_for_numbers_work_too() -> None:
    assert when("через три часа") == NOW + timedelta(hours=3)
    assert when("через пару дней") == NOW + timedelta(days=2)
    assert when("через полчаса") == NOW + timedelta(minutes=30)
    assert when("через полтора часа") == NOW + timedelta(hours=1, minutes=30)


def test_seconds_are_dropped_so_the_moment_stays_readable() -> None:
    """«Через 20 минут» от 15:00:47 = 15:20, не 15:20:47: секунды в напоминании никто не ждёт."""
    noisy = NOW.replace(second=47, microsecond=123456)
    got = when("через 20 минут", now=noisy)
    assert (got.second, got.microsecond) == (0, 0)
    assert got == NOW + timedelta(minutes=20)


@pytest.mark.parametrize("text", ["через 0 минут", "через ноль часов"])
def test_zero_interval_is_refused(text: str) -> None:
    with pytest.raises(WhenNotParsed):
        parse_when(text, now=NOW, timezone=MSK)


# ------------------------------------------------------------------ дни --


def test_day_words_take_the_default_morning_and_say_so() -> None:
    got = parse_when("завтра", now=NOW, timezone=MSK)
    assert got.at == datetime(2026, 9, 6, 9, 0, tzinfo=ZoneInfo(MSK))
    assert "09:00" in got.note, "время, придуманное парсером, обязано быть названо в ответе"


def test_day_word_with_explicit_hour_has_no_note() -> None:
    got = parse_when("завтра в 09:30", now=NOW, timezone=MSK)
    assert got.at == datetime(2026, 9, 6, 9, 30, tzinfo=ZoneInfo(MSK))
    assert got.note == "", "время назвал владелец — предупреждать не о чем"


def test_part_of_day_beats_the_default_hour() -> None:
    assert when("послезавтра вечером") == datetime(2026, 9, 7, 20, 0, tzinfo=ZoneInfo(MSK))
    assert when("завтра утром") == datetime(2026, 9, 6, 8, 0, tzinfo=ZoneInfo(MSK))
    assert note("завтра днём") == ""


def test_today_on_already_passed_time_is_refused() -> None:
    with pytest.raises(WhenNotParsed, match="ушло|прошло"):
        parse_when("сегодня в 08:00", now=NOW, timezone=MSK)


# ------------------------------------------------------------------ часы --


def test_time_only_rolls_to_next_day_with_note() -> None:
    got = parse_when("в 10:00", now=NOW, timezone=MSK)
    assert got.at == datetime(2026, 9, 6, 10, 0, tzinfo=ZoneInfo(MSK))
    assert "завтра" in got.note


def test_time_only_still_today_if_ahead() -> None:
    assert when("в 18:30") == datetime(2026, 9, 5, 18, 30, tzinfo=ZoneInfo(MSK))
    assert note("в 18:30") == ""


@pytest.mark.parametrize("text", ["в 25:00", "завтра в 90:00"])
def test_impossible_clock_time_is_refused(text: str) -> None:
    with pytest.raises(WhenNotParsed, match="время"):
        parse_when(text, now=NOW, timezone=MSK)


# ------------------------------------------------------------ дни недели --


def test_weekday_takes_the_coming_one() -> None:
    # суббота → ближайшая пятница через 6 дней
    assert when("в пятницу") == datetime(2026, 9, 11, 9, 0, tzinfo=ZoneInfo(MSK))


def test_weekday_with_time_and_part_of_day() -> None:
    assert when("в среду в 10:00") == datetime(2026, 9, 9, 10, 0, tzinfo=ZoneInfo(MSK))
    assert when("в понедельник утром") == datetime(2026, 9, 7, 8, 0, tzinfo=ZoneInfo(MSK))


def test_weekday_passed_this_week_jumps_a_week() -> None:
    # «в субботу в 09:00» в субботу вечером = следующая суббота, а не вчера
    assert when("в субботу в 09:00") == datetime(2026, 9, 12, 9, 0, tzinfo=ZoneInfo(MSK))


# ----------------------------------------------------------------- даты --


def test_explicit_dates() -> None:
    assert when("12.09.2026") == datetime(2026, 9, 12, 9, 0, tzinfo=ZoneInfo(MSK))
    assert when("12.09.2026 в 14:00") == datetime(2026, 9, 12, 14, 0, tzinfo=ZoneInfo(MSK))
    assert when("2026-09-12T08:30") == datetime(2026, 9, 12, 8, 30, tzinfo=ZoneInfo(MSK))
    assert when("2026-09-12 20:00") == datetime(2026, 9, 12, 20, 0, tzinfo=ZoneInfo(MSK))
    assert when("15 сентября в 12") == datetime(2026, 9, 15, 12, 0, tzinfo=ZoneInfo(MSK))


def test_date_part_does_not_leak_into_the_time() -> None:
    """«12.09.2026» — не «12:09». Дата маскируется перед поиском времени."""
    got = when("12.09.2026")
    assert got.hour == 9 and got.minute == 0


def test_short_date_rolls_into_next_year_when_already_past() -> None:
    assert when("01.01") == datetime(2027, 1, 1, 9, 0, tzinfo=ZoneInfo(MSK))
    assert when("20.09") == datetime(2026, 9, 20, 9, 0, tzinfo=ZoneInfo(MSK))


@pytest.mark.parametrize(
    "text",
    ["пункт 3.2", "версия 2.5", "курс 43.18", "31.02.2026", "04.09.2026", "вчера в 10"],
)
def test_not_a_date_is_refused(text: str) -> None:
    """Числа, похожие на дату, датой не становятся; прошлое не ставится никогда."""
    with pytest.raises(WhenNotParsed):
        parse_when(text, now=NOW, timezone=MSK)


@pytest.mark.parametrize("text", ["", "   ", "когда-нибудь", "напомни позвонить маме"])
def test_without_time_answer_parser_says_no(text: str) -> None:
    with pytest.raises(WhenNotParsed):
        parse_when(text, now=NOW, timezone=MSK)


# -------------------------------------------------------------- пояса --


def test_moment_belongs_to_owner_timezone_not_to_server() -> None:
    msk = parse_when("завтра в 9", now=NOW, timezone=MSK).at
    utc = parse_when("завтра в 9", now=NOW, timezone="UTC").at
    assert msk.astimezone(ZoneInfo("UTC")).strftime("%H:%M") == "06:00"
    assert utc.strftime("%H:%M") == "09:00"
    assert msk.utcoffset() == timedelta(hours=3)


def test_broken_timezone_setting_does_not_break_the_bot() -> None:
    got = parse_when("завтра в 9", now=NOW, timezone="Mars/Olympus")
    assert got.at.strftime("%H:%M") == "09:00"


def test_wall_clock_survives_a_dst_boundary() -> None:
    """«25.10 в 9:00» в поясе с переводом стрелок остаётся 9:00 по местным часам.

    Абсолютный момент в эту ночь считаем не мы (его хранит ``timestamptz``), и проверка разницы
    смещений тут deliberately не привязана к тесту: правила будущих лет зависят от tzdata образа,
    а требование к нам одно — не передвинуть владельцу часы.
    """
    cases = (("24.10.2026 в 09:00", (24, 9, 0)), ("25.10.2026 в 09:00", (25, 9, 0)))
    for text, expected in cases:
        got = when(text, tz="Europe/Berlin")
        assert (got.day, got.hour, got.minute) == expected
        assert got.utcoffset() is not None, "наивный момент утёк бы в пояс сервера"


def test_humanize_prefers_relative_then_date() -> None:
    assert humanize(NOW + timedelta(minutes=20), now=NOW, timezone=MSK) == "через 20 мин"
    assert (
        humanize(NOW + timedelta(hours=1, minutes=30), now=NOW, timezone=MSK) == "через 1 ч 30 мин"
    )
    assert humanize(NOW + timedelta(days=1, hours=1), now=NOW, timezone=MSK).startswith("завтра в")
    assert humanize(datetime(2026, 9, 20, 12, 0, tzinfo=ZoneInfo(MSK)), now=NOW, timezone=MSK) == (
        "20.09 в 12:00"
    )
    assert humanize(datetime(2027, 1, 1, 9, 0, tzinfo=ZoneInfo(MSK)), now=NOW, timezone=MSK) == (
        "01.01.2027 в 09:00"
    )


def test_humanize_of_the_past_says_the_absolute_moment() -> None:
    """Просроченное напоминание список показывает датой, а не «через -40 мин»."""
    assert humanize(NOW - timedelta(minutes=40), now=NOW, timezone=MSK) == "05.09 в 14:20"
