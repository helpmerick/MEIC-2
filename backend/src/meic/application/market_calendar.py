"""Exchange calendar — DAY-01/02/03 (pure).

Regular SPX session is 09:30–16:00 ET, weekdays, excluding holidays. Half-days
close at 13:00 ET. All times are ET (DAY-03); the caller passes an ET-aware
datetime. This is the market_open/market_halted half of the ENT-03 gate.

`ET` and `trading_day`/`trading_day_str` below are the ONE shared home for
"what ET trading day is it" (DAY-03). Every site across the codebase that
derives a today/current-day date from a clock reading must go through
`trading_day`/`trading_day_str` -- never declare a second `ZoneInfo` or roll
its own `.astimezone(...)`. This is the fix for the confirmed live bug
(2026-07-13): `datetime.now(timezone.utc).astimezone().date()` converts to
the SYSTEM's local timezone (whatever the OS/operator's machine happens to be
set to) -- not ET -- so a BST operator's local midnight (7pm ET, harmless
after the close) or a Tokyo operator's local midnight (11am ET, MID-SESSION)
silently stamps the wrong trading day onto every entry id. `composition/
live_gates.py` re-exports `ET` from here for backward compatibility; it must
never declare its own.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
HALF_DAY_CLOSE = time(13, 0)

ET = ZoneInfo("America/New_York")  # DAY-03: the ONE shared ET zone


def trading_day(now: datetime) -> date:
    """DAY-03: the ET calendar date `now` (a tz-aware instant) falls on -- the
    single source of truth for "what ET trading day is it". `now` must be
    tz-aware: a naive datetime has an unstated timezone, which is exactly the
    ambiguity that produced the live bug this function fixes -- refused
    loudly rather than silently guessed as UTC or the OS local zone.
    """
    if now.tzinfo is None:
        raise ValueError("trading_day requires a tz-aware datetime")
    return now.astimezone(ET).date()


def trading_day_str(now: datetime) -> str:
    """`trading_day` as the YYYY-MM-DD string entry ids/events are keyed by
    (`"{day}#{n}"`, see reporting/folds.py's `entry_day`)."""
    return trading_day(now).isoformat()


def is_trading_day(day: date, *, holidays: frozenset[date] = frozenset()) -> bool:
    """Weekday and not a holiday (DAY-01)."""
    return day.weekday() < 5 and day not in holidays


def next_trading_day(after: date, *, holidays: frozenset[date] = frozenset()) -> date:
    """The first trading day strictly AFTER `after` (DAY-01) — the UI-24
    weekend/holiday rollover's target day (operator ruling 2026-07-11)."""
    day = after + timedelta(days=1)
    while not is_trading_day(day, holidays=holidays):
        day += timedelta(days=1)
    return day


def session_close(day: date, *, half_days: frozenset[date] = frozenset()) -> time:
    return HALF_DAY_CLOSE if day in half_days else RTH_CLOSE


def is_market_open(
    now_et: datetime,
    *,
    holidays: frozenset[date] = frozenset(),
    half_days: frozenset[date] = frozenset(),
) -> bool:
    """True only inside the regular session of a trading day (DAY-01/02)."""
    day = now_et.date()
    if not is_trading_day(day, holidays=holidays):
        return False
    return RTH_OPEN <= now_et.time() < session_close(day, half_days=half_days)
