"""Clock implementations (the Clock port, doc 05 §6).

SystemClock is the production clock (NTP-checked in a full build, DAY-03).
MutableClock is a settable clock the demo runtime advances through a compressed
trading day — it is production code (not a test fake) so the runnable server
can drive RunTradingDay without importing test harness.
"""
from __future__ import annotations

from datetime import datetime, timezone


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class MutableClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set_time(self, when: datetime) -> None:
        self._now = when
