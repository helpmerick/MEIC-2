"""FakeClock — controllable time for the doc-04 harness.

Implements the provisional Clock port (backend/src/meic/application/ports.py).
Time only ever moves forward; `advance`/`set_time` release any `wait_until`
waiters whose deadline has been reached, which is what lets scheduler-driven
scenarios (entry windows, warm-up leads, EOD) run in compressed time.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


class FakeClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=ET)
        self._now = start
        self._waiters: list[tuple[datetime, asyncio.Future[None]]] = []

    def now(self) -> datetime:
        return self._now

    def set_time(self, when: datetime) -> None:
        if when.tzinfo is None:
            when = when.replace(tzinfo=ET)
        if when < self._now:
            raise ValueError(f"time cannot go backward: {when} < {self._now}")
        self._now = when
        self._release_due_waiters()

    def advance(self, seconds: float = 0, *, delta: timedelta | None = None) -> None:
        self.set_time(self._now + (delta if delta is not None else timedelta(seconds=seconds)))

    async def wait_until(self, when: datetime) -> None:
        if when.tzinfo is None:
            when = when.replace(tzinfo=ET)
        if when <= self._now:
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((when, fut))
        await fut

    def _release_due_waiters(self) -> None:
        still_waiting = []
        for when, fut in self._waiters:
            if when <= self._now and not fut.done():
                fut.set_result(None)
            elif not fut.done():
                still_waiting.append((when, fut))
        self._waiters = still_waiting
