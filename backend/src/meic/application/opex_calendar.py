"""Computed OpEx/quad-witch calendar events -- CAL-10 (doc 11, v1.83).

CAL-10 introduces a THIRD event class alongside CAL-01's tier-1 (fetched
official) and tier-2 (best-effort) categories: COMPUTED events, derived by
deterministic calendar math -- no fetch, no source domain, no staleness
concept. This module lives in the APPLICATION layer, not domain/, for the
same reason its DAY-01/02/03 neighbours (market_calendar.py, nyse_holidays.py)
do: domain/ must never import application/ (spec/05-architecture-ddd.md), and
this computation needs `nyse_holidays`/`prev_trading_day`/`is_trading_day` --
all application-layer pure calendar math. domain/trading_calendar.py stays
the one place that folds CAL-* events and computes tag/warning state; it
receives this module's OUTPUT (a plain `{category: dates}` mapping) as a
parameter, never importing this module itself.

Categories:
  OPEX_MONTHLY -- the monthly options expiration (third Friday of the month).
  QUAD_WITCH   -- the quarterly quadruple witching (third Friday of
                  March/June/September/December), marked visually distinct
                  from monthly OpEx. A quad-witch month gets ONLY the
                  QUAD_WITCH entry for its third Friday -- never a second,
                  separate OPEX_MONTHLY entry the same day (CAL-10: "badged
                  distinct", one computed event per day, not two stacked).

Weekly/daily expirations are deliberately EXCLUDED (CAL-10: "SPX trades 0DTE
every day, so only the elevated-activity dates carry signal").

Holiday shift: when the computed third Friday is an exchange holiday per the
DAY-01a calendar (e.g. Good Friday), the OpEx event lands on the PRECEDING
TRADING DAY -- the computation consults that YEAR's own NYSE holiday set,
never assumes Friday.
"""
from __future__ import annotations

from datetime import date

from .market_calendar import is_trading_day, prev_trading_day
from .nyse_holidays import nyse_holidays

QUAD_WITCH_MONTHS: frozenset[int] = frozenset({3, 6, 9, 12})

_FRIDAY = 4


def _third_friday(year: int, month: int) -> date:
    """The third Friday of `year`-`month` (weekday 4 = Friday)."""
    first = date(year, month, 1)
    first_friday = first.day + (_FRIDAY - first.weekday()) % 7
    return date(year, month, first_friday + 14)


def opex_events_for_year(year: int) -> dict[str, str]:
    """{"YYYY-MM-DD": "OPEX_MONTHLY" | "QUAD_WITCH"} -- exactly one entry per
    month of `year` (12 total). A month in QUAD_WITCH_MONTHS gets category
    QUAD_WITCH (never a second, separate OPEX_MONTHLY entry the same day --
    CAL-10: 'badged distinct', one computed event per day, not two stacked).
    Holiday-shifts via prev_trading_day against that YEAR's own NYSE holiday
    set. No fetch, no I/O, no clock read beyond `year` itself."""
    holidays = nyse_holidays(year)
    out: dict[str, str] = {}
    for month in range(1, 13):
        day = _third_friday(year, month)
        if not is_trading_day(day, holidays=holidays):
            day = prev_trading_day(day, holidays=holidays)
        category = "QUAD_WITCH" if month in QUAD_WITCH_MONTHS else "OPEX_MONTHLY"
        out[day.isoformat()] = category
    return out


def opex_events_for_years(years) -> dict[str, str]:
    """Union of opex_events_for_year across an iterable of years."""
    out: dict[str, str] = {}
    for year in years:
        out.update(opex_events_for_year(year))
    return out


def opex_dates_by_category(years) -> dict[str, frozenset[str]]:
    """{"OPEX_MONTHLY": frozenset(dates), "QUAD_WITCH": frozenset(dates)} --
    the shape domain/trading_calendar.py's effective_tags/active_warnings
    `computed_dates` parameter expects (per-category date set, exactly like
    a CategoryImport's own `.dates`)."""
    events = opex_events_for_years(years)
    out: dict[str, set[str]] = {"OPEX_MONTHLY": set(), "QUAD_WITCH": set()}
    for day, category in events.items():
        out[category].add(day)
    return {category: frozenset(dates) for category, dates in out.items()}
