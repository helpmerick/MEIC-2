"""NFR-08a + TPF-03g — an exit-evaluation failure ALERTS, and never blinds
the other entries in the same pass.

NFR-08a's incident: an exit evaluator threw on 15 consecutive health ticks
while EVERY exit was dead, and the session continued. The call site caught
everything and emitted a `logger.warning`, so nothing reached the operator —
the floors looked armed and were not. At 250 ms a silent throw is 240x more
frequent and no more visible.

TPF-03g is a DIFFERENT and stronger guarantee than NFR-08a, which is why both
exist: NFR-08a keeps the LOOP alive; TPF-03g keeps the PASS complete. The
2026-07-20 incident's shape was one throwing path killing everything
downstream of it in the same pass — three armed floors silently disabled by
one unmarkable leg on a fourth entry.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal as D

import pytest

from meic.adapters.api.server import _evaluate_exits_once
from meic.application.exit_alerts import ExitAlertRateLimiter, error_key
from meic.domain.events import CondorFilled, FilledLeg


# -- the rate limiter ---------------------------------------------------------

def test_nfr08a_first_error_alerts_immediately():
    lim = ExitAlertRateLimiter(window_s=60)
    assert lim.should_send("k", now_s=1000.0) is True


def test_nfr08a_repeats_of_the_same_error_are_suppressed_within_the_window():
    """At 250 ms an unlimited alert on a persistent throw is FOUR PER SECOND,
    and a channel emitting four a second is one the operator mutes -- which is
    indistinguishable from the silence NFR-08a exists to end."""
    lim = ExitAlertRateLimiter(window_s=60)
    assert lim.should_send("k", now_s=1000.0) is True
    for t in range(1, 240):                     # a minute of 250 ms passes
        assert lim.should_send("k", now_s=1000.0 + t * 0.25) is False
    assert lim.should_send("k", now_s=1060.0) is True   # window elapsed


def test_nfr08a_a_different_error_is_never_suppressed_by_another_ones_cooldown():
    """Keying on nothing would flood; keying too broadly would let a NEW
    failure be swallowed by an older one's cooldown. Two different exceptions
    are two different facts."""
    lim = ExitAlertRateLimiter(window_s=60)
    assert lim.should_send("a", now_s=1000.0) is True
    assert lim.should_send("b", now_s=1000.0) is True   # not suppressed by "a"
    assert lim.should_send("a", now_s=1000.5) is False


def test_nfr08a_a_recurrence_after_recovery_alerts_again():
    """A fault that comes back is news -- it must not inherit the cooldown
    left over from the previous episode."""
    lim = ExitAlertRateLimiter(window_s=60)
    assert lim.should_send("k", now_s=1000.0) is True
    lim.forget("k")                                     # the entry recovered
    assert lim.should_send("k", now_s=1000.1) is True


def test_nfr08a_a_zero_window_never_suppresses():
    lim = ExitAlertRateLimiter(window_s=0)
    assert lim.should_send("k", now_s=1.0) is True
    assert lim.should_send("k", now_s=1.0) is True


def test_nfr08a_error_key_separates_type_and_message():
    assert error_key("e1", ValueError("a")) != error_key("e1", ValueError("b"))
    assert error_key("e1", ValueError("a")) != error_key("e1", TypeError("a"))
    assert error_key("e1", ValueError("a")) != error_key("e2", ValueError("a"))
    assert error_key("e1", ValueError("a")) == error_key("e1", ValueError("a"))


# -- TPF-03g: one entry's failure never blinds the others ---------------------

def _legs():
    return (FilledLeg("SPXW  260720P07385000", "P", "long", 1),
            FilledLeg("SPXW  260720P07435000", "P", "short", 1),
            FilledLeg("SPXW  260720C07505000", "C", "short", 1),
            FilledLeg("SPXW  260720C07555000", "C", "long", 1))


class _Alerts:
    def __init__(self):
        self.raised = []

    def alert(self, level, message, **ctx):
        self.raised.append((level, message, ctx))


class _Clock:
    def now(self):
        from datetime import datetime, timezone
        return datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)


class _Comp:
    def __init__(self, events, floors):
        from meic.adapters.persistence.event_store import InMemoryStateStore
        from meic.application.persistent_state import PersistentState

        self.events = list(events)
        self.state = PersistentState(InMemoryStateStore())
        self.state.tpf_floors = floors
        self.state.tp_targets = {}
        self.alerts = _Alerts()
        self.clock = _Clock()


class _Commands:
    def __init__(self):
        self.closed = []

    async def close_as(self, entry_id, initiator):
        self.closed.append((entry_id, initiator))


class _ExplodingOnOneEntry:
    """An ExitMonitor that raises for exactly one entry -- the unmarkable-leg
    shape, modelled at the seam the pass actually calls."""

    def __init__(self, boom_entry):
        self._boom = boom_entry
        self.tp_confirmation_ms = 0

    def evaluate_floor(self, entry_id, *, profit_pct, level, stale, now_ms):
        if entry_id == self._boom:
            raise RuntimeError(f"cannot mark {entry_id}")
        return True                     # every other armed floor fires

    def evaluate_target(self, entry_id, **kw):
        return False

    def disarm_target(self, entry_id):
        pass

    def forget(self, entry_id):
        pass


class _Snap:
    """A non-stale snapshot holder — the pass must reach the monitor."""
    stale = False
    last = None


def test_tpf03g_one_entrys_failure_never_blinds_the_others():
    """With A, B, C armed and A raising, B and C are STILL evaluated and
    STILL fire. Without per-entry isolation, A's throw takes the whole pass
    and B and C are silently unprotected."""
    events = [CondorFilled(entry_id=e, net_credit=D("3.60"), legs=_legs())
              for e in ("A", "B", "C")]
    comp = _Comp(events, floors={"A": 90, "B": 90, "C": 90})
    commands = _Commands()

    asyncio.run(_evaluate_exits_once(comp, _Snap(), _ExplodingOnOneEntry("A"),
                                     commands, clock=comp.clock))

    fired = {eid for eid, _ in commands.closed}
    assert fired == {"B", "C"}, (
        f"expected B and C to still fire after A raised, got {fired} -- one "
        "throwing entry blinded the rest of the pass (TPF-03g)")


def test_tpf03g_the_failing_entry_is_named_in_a_critical_alert():
    """NFR-08a: the failure ALERTS, and TPF-03g requires it name THAT entry --
    an alert that does not say which entry is unprotected is not actionable."""
    events = [CondorFilled(entry_id=e, net_credit=D("3.60"), legs=_legs())
              for e in ("A", "B")]
    comp = _Comp(events, floors={"A": 90, "B": 90})
    commands = _Commands()

    asyncio.run(_evaluate_exits_once(comp, _Snap(), _ExplodingOnOneEntry("A"),
                                     commands, clock=comp.clock))

    criticals = [a for a in comp.alerts.raised if a[0] == "critical"]
    assert criticals, "a throwing entry evaluation produced no CRITICAL alert (NFR-08a)"
    assert any("A" == a[2].get("entry_id") for a in criticals), (
        "the alert does not name the failing entry (TPF-03g)")


def test_nfr08a_a_broken_alert_sink_never_kills_the_pass():
    """The sink reporting the failure must not become the failure. B must
    still fire even when alerting about A raises."""
    class _BrokenAlerts:
        def alert(self, *a, **k):
            raise RuntimeError("sink down")

    events = [CondorFilled(entry_id=e, net_credit=D("3.60"), legs=_legs())
              for e in ("A", "B")]
    comp = _Comp(events, floors={"A": 90, "B": 90})
    comp.alerts = _BrokenAlerts()
    commands = _Commands()

    asyncio.run(_evaluate_exits_once(comp, _Snap(), _ExplodingOnOneEntry("A"),
                                     commands, clock=comp.clock))

    assert ("B", "take_profit") in commands.closed
