"""NFR-08 (v1.90) CI enforcement -- TC-ENT-11 scenario "Alert sinks are wired
and thrown evaluations are heard".

BEHAVIOURAL, not a source scan (this repo has already been bitten by an
inert regex guard -- see wiring_registry.py's own module docstring on why a
heuristic proves nothing about actual wiring). This constructs a REAL
`LiveComposition` and a REAL `PaperComposition` (the same fixtures
`tests/application/test_compositions.py` uses) and asserts every
alert-raising component it holds is wired to the composition's own
`AlertRelay` -- never `None`, never a silently-swallowing no-op.

The defect this pins (2026-07-25, operator-ratified spec v1.90): `live.py`/
`paper.py` used to construct `ExecuteEntryAttempt` and `CloseEntry` with NO
`alerts=` argument at all (silently defaulting to `None`/`_NoOpAlerts`), and
`ProtectPosition` captured a direct reference to a `_NullAlerts` instance
that a later `comp.alerts = alerts` reassignment could never reach. Every one
of `ExecuteEntryAttempt`'s ORD-09/REC-01/lost-submit criticals, `CloseEntry`'s
CLS-06 partial-close criticals, and `ProtectPosition`'s STP-04 "post-fill
infeasible stop" critical were dead in both live and paper.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal as D

import pytest

from meic.application.close_entry import _NoOpAlerts
from meic.composition.alert_relay import AlertRelay
from meic.composition.live import LiveComposition
from meic.composition.paper import PaperComposition
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_clock import ET, FakeClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
CLOCK = FakeClock(datetime(2026, 7, 6, 9, 30, tzinfo=ET))


def _jwt_cert() -> str:
    import base64
    import json

    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'EdDSA'})}.{seg({'iss': 'https://api.sandbox.tastyworks.com'})}.sig"


def _live_comp() -> LiveComposition:
    return LiveComposition(clock=CLOCK, ticks=SPX, provider_secret="s", refresh_token=_jwt_cert())


def _paper_comp() -> PaperComposition:
    return PaperComposition(clock=CLOCK, ticks=SPX)


def _assert_not_swallowing(sink) -> None:
    assert sink is not None, "an alert-raising component was constructed with a None sink"
    assert not isinstance(sink, _NoOpAlerts), (
        "an alert-raising component was constructed with a silently-swallowing _NoOpAlerts sink")
    assert type(sink).__name__ != "_NullAlerts", (
        "an alert-raising component was constructed with the retired, silently-swallowing "
        "_NullAlerts sink")


# --- LiveComposition ----------------------------------------------------------

def test_live_composition_alerts_is_an_alert_relay():
    comp = _live_comp()
    assert isinstance(comp.alerts, AlertRelay)


def test_live_execute_entry_attempt_holds_the_relay():
    """The exact regression: `ExecuteEntryAttempt` used to be constructed with
    no `alerts=` kwarg at all in live.py, defaulting to `None`."""
    comp = _live_comp()
    _assert_not_swallowing(comp.execute._alerts)
    assert comp.execute._alerts is comp.alerts


def test_live_close_entry_holds_the_relay():
    """The exact regression: `CloseEntry` used to be constructed with no
    `alerts=` kwarg at all in live.py, defaulting to `_NoOpAlerts()`."""
    comp = _live_comp()
    _assert_not_swallowing(comp.close._alerts)
    assert comp.close._alerts is comp.alerts


def test_live_protect_position_holds_the_relay():
    comp = _live_comp()
    _assert_not_swallowing(comp.protect._alerts)
    assert comp.protect._alerts is comp.alerts


def test_live_composition_alerts_setter_retargets_the_same_relay_in_place():
    """`server.py`'s `comp.alerts = alerts` (the late rebind) must retarget
    the SAME relay every component captured at construction, not replace the
    composition's `alerts` attribute with a new object those components never
    see."""
    comp = _live_comp()
    relay_before = comp.alerts
    captured = []
    real_sink = type("RealSink", (), {"alert": lambda self, level, msg, **ctx: captured.append((level, msg))})()

    comp.alerts = real_sink   # the late rebind server.py performs

    assert comp.alerts is relay_before          # identity never changes
    assert comp.execute._alerts is relay_before  # every holder still sees it
    assert comp.close._alerts is relay_before
    assert comp.protect._alerts is relay_before
    comp.alerts.alert("critical", "test")
    assert ("critical", "test") in captured      # and it now actually reaches the real sink


# --- PaperComposition -----------------------------------------------------------

def test_paper_composition_alerts_is_an_alert_relay():
    comp = _paper_comp()
    assert isinstance(comp.alerts, AlertRelay)


def test_paper_execute_entry_attempt_holds_the_relay():
    comp = _paper_comp()
    _assert_not_swallowing(comp.execute._alerts)
    assert comp.execute._alerts is comp.alerts


def test_paper_close_entry_holds_the_relay():
    comp = _paper_comp()
    _assert_not_swallowing(comp.close._alerts)
    assert comp.close._alerts is comp.alerts


def test_paper_protect_position_holds_the_relay():
    comp = _paper_comp()
    _assert_not_swallowing(comp.protect._alerts)
    assert comp.protect._alerts is comp.alerts


# --- AlertRelay unit behaviour (NFR-08's own machinery) ------------------------

def test_alert_relay_with_no_target_never_swallows_and_records():
    relay = AlertRelay()
    relay.alert("critical", "boot-time critical", entry_id="e1")
    assert relay._pending == [("critical", "boot-time critical", {"entry_id": "e1"})]


def test_alert_relay_replays_pending_alerts_to_a_late_installed_target():
    """A critical raised during boot/reconcile, BEFORE `set_target` installs
    the real sink, must not be lost -- it is replayed once the target lands."""
    relay = AlertRelay()
    relay.alert("critical", "STP-04 infeasible stop", entry_id="e1")
    relay.alert("warning", "something else")

    captured = []
    sink = type("Sink", (), {"alert": lambda self, level, msg, **ctx: captured.append((level, msg, ctx))})()
    relay.set_target(sink)

    assert ("critical", "STP-04 infeasible stop", {"entry_id": "e1"}) in captured
    assert ("warning", "something else", {}) in captured
    assert relay._pending == []   # replayed entries are drained


def test_alert_relay_alerts_raised_after_set_target_go_straight_to_the_target():
    relay = AlertRelay()
    captured = []
    sink = type("Sink", (), {"alert": lambda self, level, msg, **ctx: captured.append((level, msg))})()
    relay.set_target(sink)

    relay.alert("critical", "post-install critical")

    assert ("critical", "post-install critical") in captured
    assert relay._pending == []


def test_alert_relay_a_raising_target_does_not_propagate_and_is_not_lost():
    """CLAUDE.md safety contract: an alert sink must never break a trading
    path. A target that raises must be caught, logged, and the alert kept
    recorded rather than silently dropped."""
    class _BrokenSink:
        def alert(self, level, message, **context):
            raise RuntimeError("sink is down")

    relay = AlertRelay()
    relay.set_target(_BrokenSink())

    relay.alert("critical", "must not raise out")   # must not raise

    assert relay._pending == [("critical", "must not raise out", {})]


def test_alert_relay_pending_buffer_is_bounded():
    relay = AlertRelay(cap=5)
    for i in range(20):
        relay.alert("warning", f"alert {i}")
    assert len(relay._pending) == 5
    # the newest ones survive, not the oldest
    assert relay._pending[-1][1] == "alert 19"
