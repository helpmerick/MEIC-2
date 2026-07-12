"""Hand-written step definitions for TC-STP-19 — the stop trigger derives
from the actual net fill credit (3.60), NEVER from the 3.50 working limit or
a pre-fill mid estimate. Drives the real ProtectPosition
(backend/src/meic/application/protect_position.py), constructed exactly like
tests/application/test_protect_position.py.

This scenario reuses the same 3.60 fill (1.80/1.95 shorts, 0.08/0.07 longs)
TC-ORD-08 asserts as the actual net credit, and pins the same 3.42-floors-
to-3.40 vector TC-STP-16 (vector 3) already covers at the pure
stop_policy.stop_trigger level -- confirmed here at the ProtectPosition level
too, plus the "never 3.50/mid" counterfactuals the feature calls out.
"""
import asyncio
from datetime import datetime
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.protect_position import ProtectPosition, ShortLeg
from meic.domain.stop_policy import StopBasis, stop_trigger
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import ET, FakeClock

scenarios("../features/TC-STP-19.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


class _Alerts:
    def __init__(self):
        self.calls = []

    def alert(self, level, message, **ctx):
        self.calls.append((level, message, ctx))


def _protect(broker, events, alerts, **kw):
    clock = FakeClock(datetime(2026, 7, 6, 10, 0, tzinfo=ET))
    return ProtectPosition(broker, clock, alerts, events, SPX, **kw)


@pytest.fixture
def world():
    return {}


@given("an entry filled at actual net credit 3.60 with stop_basis total_credit at 95 percent")
def _(world):
    # The same actual fill as TC-ORD-08: shorts 1.80 (PUT) + 1.95 (CALL),
    # longs 0.08 + 0.07 -> net credit 3.60.
    world["broker"], world["events"], world["alerts"] = FakeBroker(), [], _Alerts()
    world["shorts"] = [
        ShortLeg("PUT", D("1.80"), D("0.08"), symbol="SPXW  260709P07535000"),
        ShortLeg("CALL", D("1.95"), D("0.07"), symbol="SPXW  260709C07540000"),
    ]
    world["total_net_credit"] = D("3.60")
    world["pct"] = D("95")


@when("protective stops are placed")
def _(world):
    p = _protect(world["broker"], world["events"], world["alerts"])
    world["result"] = asyncio.run(p.protect(
        entry_id="e1", basis=StopBasis.TOTAL_CREDIT, shorts=world["shorts"],
        pct=world["pct"], total_net_credit=world["total_net_credit"]))


@then("each trigger = floor_to_tick(0.95 * 3.60) = 3.40")
def _(world):
    result = world["result"]
    assert result.outcome == "PROTECTED"
    assert result.triggers == {"PUT": D("3.40"), "CALL": D("3.40")}


@then("never 95 percent of the 3.50 working limit or the pre-fill mid estimate")
def _(world):
    # Counterfactual 1: 95% of the 3.50 working limit (the pre-fill rung, not
    # the actual 3.60 fill) would floor to a DIFFERENT trigger.
    from_working_limit = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"),
                                       total_net_credit=D("3.50"))
    assert from_working_limit != D("3.40")
    assert from_working_limit == D("3.30")   # floor(0.95*3.50=3.325) in the 0.10 regime

    # Counterfactual 2: 95% of a 4.00 pre-fill mid estimate would ALSO give a
    # different trigger.
    from_mid_estimate = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"),
                                      total_net_credit=D("4.00"))
    assert from_mid_estimate != D("3.40")
    assert from_mid_estimate == D("3.80")


@then("this agrees with TC-STP-16 vector 3 (3.42 floors to 3.40)")
def _(world):
    # Same pure domain function, same inputs TC-STP-16's vector 3 pins
    # (net credit 3.60 at pct 95, raw trigger 3.42) -> 3.40, never 3.50.
    vector_3 = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"), total_net_credit=D("3.60"))
    assert vector_3 == D("3.40")
    assert vector_3 != D("3.50")
    assert vector_3 == world["result"].triggers["PUT"] == world["result"].triggers["CALL"]
