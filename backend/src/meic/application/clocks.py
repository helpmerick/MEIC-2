"""Clock implementations (the Clock port, doc 05 §6).

SystemClock is the production clock (NTP-checked in a full build, DAY-03).
MutableClock is a settable clock the demo runtime advances through a compressed
trading day — it is production code (not a test fake) so the runnable server
can drive RunTradingDay without importing test harness.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def wait_until(self, when: datetime) -> None:
        """Sleep until wall-clock `when`. Required by the Clock port — LiveRuntime
        schedules entries against it. A past deadline returns immediately.

        Re-checks the deadline after each sleep so a suspended/resumed process (or
        a long sleep drifting) still wakes at the right time rather than early.
        """
        if when.tzinfo is None:
            raise ValueError("SystemClock.wait_until requires a tz-aware datetime")
        while True:
            remaining = (when - self.now()).total_seconds()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 30.0))  # cap so drift is re-checked


class MutableClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set_time(self, when: datetime) -> None:
        self._now = when
