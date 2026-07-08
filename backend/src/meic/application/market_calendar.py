"""Exchange calendar — DAY-01/02/03 (pure).

Regular SPX session is 09:30–16:00 ET, weekdays, excluding holidays. Half-days
close at 13:00 ET. All times are ET (DAY-03); the caller passes an ET-aware
datetime. This is the market_open/market_halted half of the ENT-03 gate.
"""
from __future__ import annotations

from datetime import date, datetime, time

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
HALF_DAY_CLOSE = time(13, 0)


def is_trading_day(day: date, *, holidays: frozenset[date] = frozenset()) -> bool:
    """Weekday and not a holiday (DAY-01)."""
    return day.weekday() < 5 and day not in holidays


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
