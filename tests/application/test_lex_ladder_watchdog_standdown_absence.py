"""TC-OWN-12 scenario 2 (spec/04-test-cases.md) -- the DELIBERATE ruling that
the LEX-07 watchdog is NEVER taught to trust a standdown:

  Given a standdown explanation exists for a missing long
  Then the LEX-07 watchdog still raises its alert for the operator to dismiss
  And no suppression rule keyed on standdown framing exists

This is counter-intuitive by design (OWN-12): the 07-10 incident happened
BECAUSE the bot's own explanatory text for a real LEX-07 failure ("operator
disposed of it directly -- standing down") was more convincing than the
failure itself, and masked it for three days. A `StanddownRecorded` event
existing for a side must NOT clear that side from the watchdog's pending
fold, and must NOT suppress or downgrade its alert -- a false alarm the
operator dismisses is strictly safer than a true alarm the code swallows.

These tests never construct or call anything from `stop_fill_watch.py` --
they only journal the exact event shapes that would exist in the real
incident (`ShortStopped` + `StanddownRecorded`, no `LongSaleStarted`) and
drive `lex_ladder_watchdog.py` directly, the same journal-driven-detector
discipline `test_lex_ladder_watchdog.py` already establishes.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D

from meic.application.lex_ladder_watchdog import LexLadderWatchdog, _pending_ladder_starts
from meic.domain.events import CondorFilled, ShortStopped, StanddownRecorded

ENTRY = "2026-07-10#1"
NOW = datetime(2026, 7, 14, 10, 0)


class _Alerts:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def alert(self, level, message, **ctx):
        self.calls.append((level, message, ctx))


def _condor(entry_id=ENTRY):
    return CondorFilled(entry_id=entry_id, net_credit=D("3.60"))


def _standdown_events(entry_id=ENTRY, side="CALL"):
    """The real 07-10 shape PLUS the OWN-12 journal event: a genuine stop-out
    with NO ladder ever started, and a standdown explanation on record for
    exactly this (entry_id, side)."""
    return [
        _condor(entry_id),
        ShortStopped(entry_id=entry_id, side=side, fill=D("3.85"), slippage=D("0.05"),
                     initiator="resting_stop"),
        StanddownRecorded(entry_id=entry_id, side=side,
                          reason="long_not_held_at_broker",
                          broker_finding="broker reports no open position"),
    ]


# --- scenario 2: the fold is structurally blind to StanddownRecorded ----------

def test_pending_ladder_starts_ignores_standdown_recorded_entirely():
    """Structural pin: `_pending_ladder_starts` must not even IMPORT
    `StanddownRecorded` -- the (entry_id, side) stays pending whether or not
    a standdown explanation is on the log, because the fold's only inputs
    are ShortStopped/LongSaleStarted/LongSold/SideClosed/EntryClosed."""
    events = _standdown_events()
    assert (ENTRY, "CALL") in _pending_ladder_starts(events)


def test_watchdog_still_alerts_past_grace_with_a_standdown_explanation_present():
    """TC-OWN-12 scenario 2, the operative assertion: 'the LEX-07 watchdog
    still raises its alert for the operator to dismiss' -- a standdown event
    existing changes NOTHING about whether/when the alert fires."""
    alerts = _Alerts()
    wd = LexLadderWatchdog(alerts=alerts, grace_seconds=D("60"))
    events = _standdown_events()

    wd.observe(events, now=NOW)
    assert alerts.calls == [], "still inside the grace window"

    wd.observe(events, now=NOW + timedelta(seconds=61))
    assert len(alerts.calls) == 1
    level, message, ctx = alerts.calls[0]
    assert level == "critical"
    assert "LEX-07" in message
    assert ctx["entry_id"] == ENTRY and ctx["side"] == "CALL"


def test_watchdog_alerts_identically_with_and_without_the_standdown_event():
    """Same journal, same grace window, same alert -- the ONLY difference
    between the two logs is the presence of `StanddownRecorded`. If a
    suppression rule were ever added keyed on that event, this pair would
    diverge (empty vs one alert); today they must be identical."""
    def _run(events):
        alerts = _Alerts()
        wd = LexLadderWatchdog(alerts=alerts, grace_seconds=D("60"))
        wd.observe(events, now=NOW)                          # first sighting
        wd.observe(events, now=NOW + timedelta(seconds=61))  # past the grace window
        return alerts.calls

    without_standdown = [
        _condor(),
        ShortStopped(entry_id=ENTRY, side="CALL", fill=D("3.85"), slippage=D("0.05"),
                     initiator="resting_stop"),
    ]
    with_standdown = _standdown_events()

    calls_without = _run(without_standdown)
    calls_with = _run(with_standdown)

    assert len(calls_without) == 1
    assert len(calls_with) == 1
    # Same level/message/entry/side either way -- the standdown event is
    # invisible to this watchdog's decision, by design (OWN-12).
    assert calls_without[0][:2] == calls_with[0][:2]
    assert calls_without[0][2]["entry_id"] == calls_with[0][2]["entry_id"]
    assert calls_without[0][2]["side"] == calls_with[0][2]["side"]


# --- the ABSENCE test TC-OWN-12 explicitly demands ----------------------------

def test_no_suppression_rule_keyed_on_standdown_framing_exists():
    """TC-OWN-12 scenario 2's second assertion, made a REAL structural check
    rather than a comment: `_pending_ladder_starts` must not consult
    `StanddownRecorded` at all, and a journaled standdown must NOT clear a
    pending (entry_id, side) from the set the watchdog alerts on.

    This is written to FAIL the moment a future change teaches the fold to
    treat a standdown as a legal exemption (mirroring DCY-03's `initiator ==
    "decay"` check) -- e.g. adding a branch like `if key in standdown_keys:
    continue` next to the DCY-03 skip. Rationale (OWN-12, operator-ratified):
    suppression would teach the watchdog to trust exactly the explanation
    class that masked the 07-10 failure for three days; a false alarm the
    operator dismisses is strictly safer than a true alarm the code
    swallows."""
    import inspect

    from meic.application import lex_ladder_watchdog as mod

    # (1) The module must not import StanddownRecorded at all -- if a future
    # agent adds the import, that is already the first step toward wiring in
    # a suppression rule, and this assertion catches it immediately.
    assert "StanddownRecorded" not in dir(mod), (
        "lex_ladder_watchdog.py must never import StanddownRecorded -- OWN-12 "
        "forbids any suppression keyed on standdown framing")

    # (2) The fold's own source text must never mention "standdown" in any
    # form -- the DCY-03 exemption is spelled `initiator == "decay"` and is
    # legitimate (a decay-initiated stop has no ladder at all); a standdown
    # exemption would need its own keyword, and none may exist.
    source = inspect.getsource(mod)
    assert "standdown" not in source.lower(), (
        "lex_ladder_watchdog.py's source must never reference standdowns -- "
        "any such reference is the suppression rule OWN-12 forbids")

    # (3) Behavioural pin, independent of (1)/(2): a pending (entry_id, side)
    # with a StanddownRecorded on the log is STILL pending -- the event must
    # not clear it.
    events = _standdown_events()
    assert (ENTRY, "CALL") in _pending_ladder_starts(events)
