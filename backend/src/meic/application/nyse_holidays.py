"""NYSE holiday calendar — DAY-01/02 (pure).

The exchange's full-day holidays and early-close half-days, computed
algorithmically for any year. These are exchange FACTS, not operator
configuration (operator ruling 2026-07-11): the rules haven't changed since
Juneteenth was added in 2022, and computing them beats a hand-maintained list
that silently goes stale every January.

Full-day holidays (with the NYSE observance shifts, Saturday -> Friday and
Sunday -> Monday): New Year's Day, MLK Day, Presidents' Day, Good Friday,
Memorial Day, Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas.
One deliberate quirk is encoded: a New Year's Day that falls on Saturday is NOT
observed on the prior Friday (that would move the holiday into the previous
year — the exchange simply stays open, e.g. Friday 2021-12-31).

Half-days (13:00 ET close, `market_calendar.session_close`): July 3rd and
Christmas Eve when they fall Monday-Thursday, and the day after Thanksgiving.
"""
from __future__ import annotations

from datetime import date, timedelta

_MON, _THU, _SAT, _SUN = 0, 3, 5, 6


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (Meeus/Jones/Butcher computus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 — the algorithm's own name
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_monday_of_may(year: int) -> date:
    d = date(year, 5, 31)
    return d - timedelta(days=(d.weekday() - _MON) % 7)


def _observed(d: date) -> date:
    """NYSE observance shift: Saturday -> Friday, Sunday -> Monday."""
    if d.weekday() == _SAT:
        return d - timedelta(days=1)
    if d.weekday() == _SUN:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> frozenset[date]:
    """Every full-day NYSE holiday observed in `year` (DAY-01)."""
    days = set()
    for month, day in ((1, 1), (6, 19), (7, 4), (12, 25)):
        fixed = date(year, month, day)
        # New Year's on a Saturday is not shifted back into the prior year:
        # no observance at all (the exchange stayed open on 2021-12-31).
        if (month, day) == (1, 1) and fixed.weekday() == _SAT:
            continue
        days.add(_observed(fixed))
    days.add(_nth_weekday(year, 1, _MON, 3))     # MLK Day
    days.add(_nth_weekday(year, 2, _MON, 3))     # Presidents' Day
    days.add(_easter(year) - timedelta(days=2))  # Good Friday
    days.add(_last_monday_of_may(year))          # Memorial Day
    days.add(_nth_weekday(year, 9, _MON, 1))     # Labor Day
    days.add(_nth_weekday(year, 11, _THU, 4))    # Thanksgiving
    return frozenset(days)


def nyse_half_days(year: int) -> frozenset[date]:
    """Early-close (13:00 ET) sessions in `year` (DAY-02)."""
    days = {_nth_weekday(year, 11, _THU, 4) + timedelta(days=1)}  # day after Thanksgiving
    for month, day in ((7, 3), (12, 24)):
        eve = date(year, month, day)
        # Monday-Thursday only: on a Friday the eve is itself the OBSERVED
        # holiday (July 4th / Christmas on Saturday), and on a weekend there
        # is no session to shorten.
        if eve.weekday() <= _THU:
            days.add(eve)
    return frozenset(days)


def holidays_near(day: date, *, years_ahead: int = 1) -> frozenset[date]:
    """Holidays for `day`'s year through `years_ahead` more — enough for any
    forward scan (a next-trading-day walk near New Year needs January of the
    year after)."""
    out: set[date] = set()
    for y in range(day.year, day.year + years_ahead + 1):
        out |= nyse_holidays(y)
    return frozenset(out)


def half_days_near(day: date, *, years_ahead: int = 1) -> frozenset[date]:
    """`nyse_half_days` over the same window as `holidays_near`."""
    out: set[date] = set()
    for y in range(day.year, day.year + years_ahead + 1):
        out |= nyse_half_days(y)
    return frozenset(out)
