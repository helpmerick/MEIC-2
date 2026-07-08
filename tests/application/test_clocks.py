"""SystemClock — the production Clock port. LiveRuntime schedules against it, so
wait_until must exist and behave (a missing wait_until crashes a live day)."""
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from meic.application.clocks import SystemClock
from meic.composition.live_gates import LiveMarketGates

ET = ZoneInfo("America/New_York")


def test_system_clock_now_is_tz_aware_utc():
    now = SystemClock().now()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


def test_wait_until_returns_immediately_for_a_past_deadline():
    clock = SystemClock()
    past = clock.now() - timedelta(hours=1)
    started = clock.now()
    asyncio.run(clock.wait_until(past))
    assert (clock.now() - started).total_seconds() < 1  # did not sleep


def test_wait_until_sleeps_until_the_deadline():
    clock = SystemClock()
    target = clock.now() + timedelta(milliseconds=120)
    asyncio.run(clock.wait_until(target))
    assert clock.now() >= target  # woke at or after the deadline, never early


def test_wait_until_requires_tz_aware():
    with pytest.raises(ValueError, match="tz-aware"):
        asyncio.run(SystemClock().wait_until(datetime(2026, 7, 8, 10, 0)))


# --- the calendar gate must read the UTC clock as ET (DAY-03) -----------------

class _FixedClock:
    def __init__(self, now): self._now = now
    def now(self): return self._now


async def _ok():
    return True


def _gates_at(utc_dt):
    return asyncio.run(LiveMarketGates(clock=_FixedClock(utc_dt), data_fresh=_ok,
                                       session_valid=_ok, buying_power_ok=_ok)())


def test_utc_clock_is_interpreted_as_et_for_market_hours():
    # 14:00 UTC == 10:00 ET (EDT) -> open
    assert _gates_at(datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc)).market_open is True
    # 12:00 UTC == 08:00 ET -> pre-open, closed
    assert _gates_at(datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)).market_open is False
    # 21:00 UTC == 17:00 ET -> after the close
    assert _gates_at(datetime(2026, 7, 8, 21, 0, tzinfo=timezone.utc)).market_open is False


def test_gates_reject_a_naive_clock():
    with pytest.raises(ValueError, match="tz-aware"):
        _gates_at(datetime(2026, 7, 8, 14, 0))
