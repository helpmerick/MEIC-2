"""LEX-07 invariant watchdog (2026-07-14) -- the CLASS-level fix for the
2026-07-10 incident: `ShortStopped(entry_id="2026-07-10#1", side="CALL")` was
journaled and then NOTHING -- no `LongSaleStarted`, no `LexOrderPlaced`, no
`LongSold`, no `SideClosed`. The LEX ladder never ran, and nothing that
inspected events for MISTAKES could see it, because a component that never
runs emits no wrong events -- it emits no events at all.

These tests pin `application/lex_ladder_watchdog.py`: a PURE fold over the
journal (`_pending_ladder_starts`) plus a bounded-grace-window, dedup'd
alerting wrapper (`LexLadderWatchdog.observe`). Deliberately journal-driven,
never a hook inside the LEX code path -- these tests never construct or call
anything from `recover_long.py`/`stop_fill_watch.py`, proving the watchdog
needs none of it to catch the gap.
"""
from datetime import datetime, timedelta
from decimal import Decimal as D

from meic.application.lex_ladder_watchdog import LexLadderWatchdog, _pending_ladder_starts
from meic.domain.events import (
    CondorFilled,
    EntryClosed,
    LexOrderPlaced,
    LongSaleRepriced,
    LongSaleStarted,
    LongSold,
    ShortStopped,
    SideClosed,
)

ENTRY = "2026-07-10#1"
NOW = datetime(2026, 7, 14, 10, 0)


class _Alerts:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def alert(self, level, message, **ctx):
        self.calls.append((level, message, ctx))


def _condor(entry_id=ENTRY):
    return CondorFilled(entry_id=entry_id, net_credit=D("3.60"))


# --- the real 2026-07-10 shape: ShortStopped, then NOTHING -------------------

def test_stop_out_with_no_ladder_start_alerts_critical_naming_entry_and_side_past_grace():
    """LEX-07: a genuine stop-out (`initiator='resting_stop'`) with no
    `LongSaleStarted` at all must alert CRITICAL, naming the entry+side, once
    the grace window has elapsed. This is the exact 2026-07-10 shape."""
    events = [_condor(), ShortStopped(entry_id=ENTRY, side="CALL", fill=D("3.80"),
                                      slippage=D("0"), initiator="resting_stop")]
    alerts = _Alerts()
    wd = LexLadderWatchdog(alerts=alerts, grace_seconds=D("60"))

    wd.observe(events, now=NOW)  # first sighting -- must not alert yet
    assert alerts.calls == []

    wd.observe(events, now=NOW + timedelta(seconds=60))  # grace elapsed
    assert len(alerts.calls) == 1
    level, message, ctx = alerts.calls[0]
    assert level == "critical"
    assert "LEX-07" in message
    assert ctx["entry_id"] == ENTRY
    assert ctx["side"] == "CALL"


# --- the healthy 2026-07-13 shape: full ladder journaled --------------------

def test_full_ladder_journaled_never_alerts():
    events = [
        _condor(),
        ShortStopped(entry_id=ENTRY, side="CALL", fill=D("3.80"), slippage=D("0"),
                     initiator="resting_stop"),
        LongSaleStarted(entry_id=ENTRY, side="CALL"),
        LexOrderPlaced(entry_id=ENTRY, side="CALL", broker_order_id="o1",
                       price=D("0.10"), kind="ladder"),
        LongSaleRepriced(entry_id=ENTRY, side="CALL", step=1, price=D("0.08")),
        LongSold(entry_id=ENTRY, side="CALL", recovery=D("0.08")),
        SideClosed(entry_id=ENTRY, side="CALL"),
    ]
    alerts = _Alerts()
    wd = LexLadderWatchdog(alerts=alerts, grace_seconds=D("60"))

    for tick in (0, 60, 120, 600):
        wd.observe(events, now=NOW + timedelta(seconds=tick))

    assert alerts.calls == []


# --- DCY-03: decay buyback, long deliberately left to expire ----------------

def test_dcy03_decay_initiated_stop_with_no_ladder_never_alerts():
    """DCY-03: after a decay buyback the long is DELIBERATELY left to expire
    -- LEX-07's always-sell is for stop-outs, not decay closes. A decay
    `ShortStopped` with no `LongSaleStarted` at all must NEVER alert, even far
    past the grace window -- getting this wrong trains the operator to ignore
    the alarm on every correct decay close."""
    events = [
        _condor(),
        ShortStopped(entry_id=ENTRY, side="PUT", fill=D("0.05"), slippage=D("0"),
                     initiator="decay"),
        EntryClosed(entry_id=ENTRY, initiator="decay"),
    ]
    alerts = _Alerts()
    wd = LexLadderWatchdog(alerts=alerts, grace_seconds=D("60"))

    for tick in (0, 60, 600, 6000):
        wd.observe(events, now=NOW + timedelta(seconds=tick))

    assert alerts.calls == []


def test_dcy03_decay_stop_alone_never_alerts_even_without_the_entryclosed_event():
    """Defence in depth: decay_watcher/stop_fill_watch append `ShortStopped`
    and `EntryClosed` as two SEPARATE list appends -- a crash between them
    would leave a decay `ShortStopped` journaled alone. The watchdog must
    still recognise `initiator == 'decay'` directly and stay silent, not rely
    on `EntryClosed` having also landed."""
    events = [_condor(), ShortStopped(entry_id=ENTRY, side="PUT", fill=D("0.05"),
                                      slippage=D("0"), initiator="decay")]
    alerts = _Alerts()
    wd = LexLadderWatchdog(alerts=alerts, grace_seconds=D("60"))

    for tick in (0, 60, 600):
        wd.observe(events, now=NOW + timedelta(seconds=tick))

    assert alerts.calls == []


# --- inside the grace window: no premature fire -----------------------------

def test_inside_grace_window_does_not_alert_yet():
    events = [_condor(), ShortStopped(entry_id=ENTRY, side="CALL", fill=D("3.80"),
                                      slippage=D("0"), initiator="resting_stop")]
    alerts = _Alerts()
    wd = LexLadderWatchdog(alerts=alerts, grace_seconds=D("60"))

    wd.observe(events, now=NOW)
    wd.observe(events, now=NOW + timedelta(seconds=30))
    wd.observe(events, now=NOW + timedelta(seconds=59))

    assert alerts.calls == []


# --- one alert per (entry, side), never per tick -----------------------------

def test_alerts_exactly_once_per_entry_side_across_many_repeated_ticks():
    events = [_condor(), ShortStopped(entry_id=ENTRY, side="CALL", fill=D("3.80"),
                                      slippage=D("0"), initiator="resting_stop")]
    alerts = _Alerts()
    wd = LexLadderWatchdog(alerts=alerts, grace_seconds=D("60"))

    for tick in range(0, 600, 30):  # far past the grace window, many ticks
        wd.observe(events, now=NOW + timedelta(seconds=tick))

    assert len(alerts.calls) == 1, "must never re-alert the same (entry, side) on later ticks"


# --- terminal states: LongSold / SideClosed suppress the alert --------------

def test_long_sold_without_prior_longsalestarted_still_suppresses_alert():
    """Even if `LongSaleStarted` itself is somehow missing, reaching a
    terminal LEX state (`LongSold`) means the invariant this watchdog guards
    was satisfied in substance -- never alert."""
    events = [_condor(), ShortStopped(entry_id=ENTRY, side="CALL", fill=D("3.80"),
                                      slippage=D("0"), initiator="resting_stop"),
              LongSold(entry_id=ENTRY, side="CALL", recovery=D("0.08"))]
    alerts = _Alerts()
    wd = LexLadderWatchdog(alerts=alerts, grace_seconds=D("60"))

    for tick in (0, 60, 600):
        wd.observe(events, now=NOW + timedelta(seconds=tick))

    assert alerts.calls == []


def test_side_closed_without_prior_longsalestarted_still_suppresses_alert():
    events = [_condor(), ShortStopped(entry_id=ENTRY, side="PUT", fill=D("3.80"),
                                      slippage=D("0"), initiator="resting_stop"),
              SideClosed(entry_id=ENTRY, side="PUT")]
    alerts = _Alerts()
    wd = LexLadderWatchdog(alerts=alerts, grace_seconds=D("60"))

    for tick in (0, 60, 600):
        wd.observe(events, now=NOW + timedelta(seconds=tick))

    assert alerts.calls == []


def test_entry_closed_by_operator_suppresses_alert_even_with_no_ladder():
    """CLS: an entry closed by the operator via CloseEntry disposes of both
    sides outside the ladder -- must not alert."""
    events = [_condor(), ShortStopped(entry_id=ENTRY, side="CALL", fill=D("3.80"),
                                      slippage=D("0"), initiator="resting_stop"),
              EntryClosed(entry_id=ENTRY, initiator="manual")]
    alerts = _Alerts()
    wd = LexLadderWatchdog(alerts=alerts, grace_seconds=D("60"))

    for tick in (0, 60, 600):
        wd.observe(events, now=NOW + timedelta(seconds=tick))

    assert alerts.calls == []


# --- pure fold sanity (no watchdog/alerts involved) -------------------------

def test_pending_ladder_starts_pure_fold_matches_expectations():
    events = [
        _condor(entry_id="a"), ShortStopped(entry_id="a", side="CALL", fill=D("1"),
                                            slippage=D("0"), initiator="resting_stop"),
        _condor(entry_id="b"), ShortStopped(entry_id="b", side="PUT", fill=D("1"),
                                            slippage=D("0"), initiator="decay"),
        _condor(entry_id="c"), ShortStopped(entry_id="c", side="PUT", fill=D("1"),
                                            slippage=D("0"), initiator="resting_stop"),
        LongSaleStarted(entry_id="c", side="PUT"),
    ]
    pending = _pending_ladder_starts(events)
    assert pending == {("a", "CALL")}


# --- two independent sides of the same entry are tracked separately --------

def test_whipsaw_both_sides_stopped_tracked_independently():
    """STP-08: both sides of one condor can stop the same day. One side's
    healthy ladder must never mask the other side's silently-missing one."""
    events = [
        _condor(),
        ShortStopped(entry_id=ENTRY, side="PUT", fill=D("1"), slippage=D("0"),
                     initiator="resting_stop"),
        LongSaleStarted(entry_id=ENTRY, side="PUT"),
        ShortStopped(entry_id=ENTRY, side="CALL", fill=D("1"), slippage=D("0"),
                     initiator="resting_stop"),
    ]
    alerts = _Alerts()
    wd = LexLadderWatchdog(alerts=alerts, grace_seconds=D("60"))

    wd.observe(events, now=NOW)
    wd.observe(events, now=NOW + timedelta(seconds=60))

    assert len(alerts.calls) == 1
    assert alerts.calls[0][2]["side"] == "CALL"
