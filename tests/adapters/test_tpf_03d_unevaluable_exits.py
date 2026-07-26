"""TPF-03d — an armed exit that cannot be evaluated is SURFACED, never silent.
Plus TPF-03b(iii): a clock that is not advancing is the same observable.

THE NFR-09 SHAPE, from the rule itself: an unmarkable leg makes the cost
function return None, which reads downstream as "no breach" -- INDISTINGUISHABLE
from "not breached". The operator believes a floor is protecting them while it
has not been evaluated for hours. Nothing errors. Nothing fires. It is the
60 s blind window's quieter cousin.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal as D

from meic.adapters.api.server import _evaluate_exits_once
from meic.application.exit_evaluability import ClockStallDetector, ExitEvaluabilityTracker
from meic.domain.events import CondorFilled, FilledLeg


# -- the tracker --------------------------------------------------------------

def test_tpf03d_no_alert_before_the_threshold():
    t = ExitEvaluabilityTracker(alert_after_s=60)
    assert t.observe("e1", evaluable=False, reason="no mark", now_s=0) is None
    assert t.observe("e1", evaluable=False, reason="no mark", now_s=59) is None


def test_tpf03d_alerts_once_the_condition_persists():
    t = ExitEvaluabilityTracker(alert_after_s=60)
    t.observe("e1", evaluable=False, reason="no mark", now_s=0)
    out = t.observe("e1", evaluable=False, reason="no mark", now_s=60)
    assert out is not None
    assert out.entry_id == "e1" and "no mark" in out.reason
    assert out.seconds >= 60


def test_tpf03d_alerts_once_per_episode_not_once_per_pass():
    """At 250 ms a per-pass alert is FOUR PER SECOND. The rate limiter is a
    backstop, not the only thing between the operator and a flood."""
    t = ExitEvaluabilityTracker(alert_after_s=60)
    t.observe("e1", evaluable=False, reason="no mark", now_s=0)
    assert t.observe("e1", evaluable=False, reason="no mark", now_s=60) is not None
    for ms in range(1, 400):
        assert t.observe("e1", evaluable=False, reason="no mark",
                         now_s=60 + ms * 0.25) is None


def test_tpf03d_recovery_CLEARS_so_a_flickering_leg_never_accumulates():
    """CONTINUOUSLY is the operative word. An accumulator would eventually
    alert on a leg that was fine almost all the time."""
    t = ExitEvaluabilityTracker(alert_after_s=60)
    t.observe("e1", evaluable=False, reason="no mark", now_s=0)
    t.observe("e1", evaluable=True, now_s=59)          # recovers just in time
    # The new episode starts at 60, so the full threshold runs again from
    # there -- 119 is 59s in and must stay silent; 120 is the first moment the
    # condition has genuinely persisted for a minute WITHOUT interruption.
    assert t.observe("e1", evaluable=False, reason="no mark", now_s=60) is None
    assert t.observe("e1", evaluable=False, reason="no mark", now_s=119) is None
    assert t.observe("e1", evaluable=False, reason="no mark", now_s=120) is not None


def test_tpf03d_a_genuine_recurrence_alerts_again():
    """Blind, recovered, blind again is TWO incidents."""
    t = ExitEvaluabilityTracker(alert_after_s=10)
    t.observe("e1", evaluable=False, reason="no mark", now_s=0)
    assert t.observe("e1", evaluable=False, reason="no mark", now_s=10) is not None
    t.observe("e1", evaluable=True, now_s=11)
    t.observe("e1", evaluable=False, reason="no mark", now_s=12)
    assert t.observe("e1", evaluable=False, reason="no mark", now_s=22) is not None


def test_tpf03d_the_card_shows_it_immediately_while_the_alert_waits():
    """Showing a degraded exit at once, but only INTERRUPTING once it
    persists, is the difference between a useful indicator and a noisy one."""
    t = ExitEvaluabilityTracker(alert_after_s=60)
    assert t.observe("e1", evaluable=False, reason="no mark", now_s=0) is None
    state = t.state_for("e1", now_s=1)
    assert state is not None and state.reason == "no mark" and state.seconds == 1


def test_tpf03d_an_evaluable_entry_has_no_card_state():
    t = ExitEvaluabilityTracker(alert_after_s=60)
    t.observe("e1", evaluable=True, now_s=0)
    assert t.state_for("e1", now_s=100) is None


def test_tpf03d_forget_drops_the_episode_entirely():
    t = ExitEvaluabilityTracker(alert_after_s=1)
    t.observe("e1", evaluable=False, reason="no mark", now_s=0)
    t.forget("e1")
    assert t.state_for("e1", now_s=100) is None


# -- TPF-03b(iii): the clock itself -------------------------------------------

def test_tpf03biii_first_reading_is_not_a_stall():
    """There is no prior reading to compare; treating "no evidence yet" as a
    stall would alert on every boot."""
    assert ClockStallDetector().advanced(1000) is True


def test_tpf03biii_a_repeating_clock_is_a_stall():
    """A now_ms that repeats cannot accumulate a continuous breach, so every
    armed floor silently stops being able to confirm while looking healthy.
    A required argument catches an ABSENT clock; only this catches a STOPPED
    one."""
    d = ClockStallDetector()
    d.advanced(1000)
    assert d.advanced(1000) is False
    assert d.advanced(1001) is True


def test_tpf03biii_a_backwards_clock_is_a_stall():
    d = ClockStallDetector()
    d.advanced(1000)
    assert d.advanced(999) is False


# -- end to end through the evaluation pass -----------------------------------

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
    def __init__(self):
        self.t = 1_000_000.0

    def now(self):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(self.t, tz=timezone.utc)


class _Comp:
    def __init__(self, floors, alert_after_s=60):
        from meic.adapters.persistence.event_store import InMemoryStateStore
        from meic.application.persistent_state import PersistentState

        self.events = [CondorFilled(entry_id=e, net_credit=D("3.60"), legs=_legs())
                       for e in floors]
        self.state = PersistentState(InMemoryStateStore())
        self.state.tpf_floors = floors
        self.state.tp_targets = {}
        self.alerts = _Alerts()
        self.clock = _Clock()
        self.exit_unevaluable_alert_s = alert_after_s


class _Commands:
    def __init__(self):
        self.closed = []

    async def close_as(self, entry_id, initiator):
        self.closed.append((entry_id, initiator))


class _StaleSnap:
    """No usable mark -- the unmarkable-leg condition."""
    stale = True
    last = None


class _Monitor:
    tp_confirmation_ms = 0

    def evaluate_floor(self, entry_id, **kw):
        return False

    def evaluate_target(self, entry_id, **kw):
        return False

    def disarm_target(self, entry_id):
        pass

    def forget(self, entry_id):
        pass


def test_tpf03d_a_persistently_unmarkable_armed_floor_raises_a_critical():
    """The pass itself, not just the tracker: an armed floor that cannot be
    marked for the whole threshold must reach the operator."""
    comp = _Comp({"e1": 20}, alert_after_s=60)
    commands = _Commands()

    asyncio.run(_evaluate_exits_once(comp, _StaleSnap(), _Monitor(), commands,
                                     clock=comp.clock))
    assert comp.alerts.raised == [], "alerted before the threshold elapsed"

    comp.clock.t += 61                                   # the condition persists
    asyncio.run(_evaluate_exits_once(comp, _StaleSnap(), _Monitor(), commands,
                                     clock=comp.clock))

    criticals = [a for a in comp.alerts.raised if a[0] == "critical"]
    assert criticals, "an armed floor was unevaluable for a minute and said nothing"
    assert criticals[0][2].get("entry_id") == "e1", "the alert must name the entry"
    assert "UNEVALUABLE" in criticals[0][1].upper()
    assert commands.closed == [], "nothing should have fired"
