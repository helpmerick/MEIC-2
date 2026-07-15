"""CalendarStore fail-open behaviour -- CAL-07, plus final-review finding 2
(2026-07-15): failing OPEN is ruled; failing SILENT is not.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from meic.application.calendar_store import CalendarStore
from tests.harness.fake_clock import FastClock

NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


class _BrokenEvents:
    """An event source whose iteration blows up -- the 'unreadable store'
    case CAL-07 rules must fail OPEN (trade), never closed."""

    def __iter__(self):
        raise RuntimeError("journal corrupted (synthetic)")


def test_cal07_unreadable_store_fails_open_and_logs(caplog):
    store = CalendarStore(_BrokenEvents(), FastClock(NOW))

    with caplog.at_level(logging.ERROR, logger="meic.application.calendar_store"):
        assert store.label_for_day("2026-07-15") is None   # CAL-07: fail-open

    # ... but NEVER silently (finding 2): exactly one traceback-carrying
    # record names the failure, so a fold bug can't disable every blackout
    # forever without a line in the log.
    records = [r for r in caplog.records if "CAL-07 fail-open" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is not None                 # full traceback attached
    assert "2026-07-15" in records[0].getMessage()


def test_cal07_a_healthy_empty_store_is_silent(caplog):
    """The fail-open LOG is for failures only -- an ordinary empty calendar
    (the everyday CAL-07 case) reads None with no log noise."""
    store = CalendarStore([], FastClock(NOW))
    with caplog.at_level(logging.ERROR, logger="meic.application.calendar_store"):
        assert store.label_for_day("2026-07-15") is None
    assert caplog.records == []
