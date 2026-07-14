"""ORD-11 (v1.67): `lex_ladder_watchdog.py` prefers a pending side's OWN
`ShortStopped.at` over wall-clock first-sighting when deciding whether the
grace window has elapsed -- so a PROCESS RESTART no longer resets the grace
window (the exact gap flagged in stop_fill_watch.py's module docstring and
the 07-13 review: "the watchdog currently has to track wall-clock
first-sighting because events carry no time, so a process restart resets
its grace window").

Legacy events (`at=None`) must keep using the pre-ORD-11 wall-clock
`_first_seen` tracking, unchanged -- the live journal is full of them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from meic.application.lex_ladder_watchdog import LexLadderWatchdog
from meic.domain.events import CondorFilled, ShortStopped

ENTRY = "2026-07-10#1"


class _Alerts:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def alert(self, level, message, **ctx):
        self.calls.append((level, message, ctx))


def test_event_at_survives_a_simulated_restart_no_grace_window_reset():
    """A fresh `LexLadderWatchdog` (simulating a process restart) that first
    observes the pending side 90s AFTER its own `ShortStopped.at` must fire
    IMMEDIATELY -- past the 60s grace window already, per the event's own
    timestamp -- rather than starting a brand-new 60s clock from `now`."""
    stop_at = datetime(2026, 7, 10, 15, 56, 15, tzinfo=timezone.utc)
    events = [
        CondorFilled(entry_id=ENTRY, net_credit=D("3.60")),
        ShortStopped(entry_id=ENTRY, side="CALL", fill=D("3.85"), slippage=D("0.05"),
                    initiator="resting_stop", at=stop_at.isoformat()),
    ]
    alerts = _Alerts()
    wd = LexLadderWatchdog(alerts=alerts, grace_seconds=D("60"))  # fresh instance == "restarted"

    now = stop_at + timedelta(seconds=90)
    wd.observe(events, now=now)

    assert len(alerts.calls) == 1, (
        "ORD-11: the event's own `at` must anchor the grace window -- a fresh "
        "watchdog instance must not get a brand-new 60s allowance from `now`")


def test_event_at_still_respects_the_grace_window_when_not_yet_elapsed():
    stop_at = datetime(2026, 7, 10, 15, 56, 15, tzinfo=timezone.utc)
    events = [
        CondorFilled(entry_id=ENTRY, net_credit=D("3.60")),
        ShortStopped(entry_id=ENTRY, side="CALL", fill=D("3.85"), slippage=D("0.05"),
                    initiator="resting_stop", at=stop_at.isoformat()),
    ]
    alerts = _Alerts()
    wd = LexLadderWatchdog(alerts=alerts, grace_seconds=D("60"))

    wd.observe(events, now=stop_at + timedelta(seconds=30))
    assert alerts.calls == [], "only 30s of the 60s grace window has elapsed since the event's own at"


def test_legacy_event_with_at_none_falls_back_to_wall_clock_first_seen():
    """The pre-ORD-11 behaviour, UNCHANGED: an event with no `at` (every
    entry in the live journal before this ships) anchors the grace window to
    whenever THIS process first saw it pending -- exactly like today."""
    now = datetime(2026, 7, 14, 10, 0)
    events = [
        CondorFilled(entry_id=ENTRY, net_credit=D("3.60")),
        ShortStopped(entry_id=ENTRY, side="CALL", fill=D("3.80"), slippage=D("0"),
                    initiator="resting_stop"),  # at=None
    ]
    alerts = _Alerts()
    wd = LexLadderWatchdog(alerts=alerts, grace_seconds=D("60"))

    wd.observe(events, now=now)
    assert alerts.calls == [], "first sighting -- grace window starts NOW, not in the past"
    wd.observe(events, now=now + timedelta(seconds=61))
    assert len(alerts.calls) == 1
