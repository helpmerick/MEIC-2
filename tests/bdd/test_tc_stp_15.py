"""Hand-written step definitions for TC-STP-15 — STP-02c feasibility guard."""
import asyncio
from datetime import datetime
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.entry_gates import GateSnapshot
from meic.application.protect_position import ProtectPosition, ShortLeg
from meic.domain.stop_policy import StopBasis, feasible
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker, Scripted
from tests.harness.fake_clock import ET, FakeClock

scenarios("../features/TC-STP-15.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
GATES_PASS = GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                          flatten_in_progress=False, market_open=True, market_halted=False,
                          data_fresh=True, session_valid=True, buying_power_ok=True)


class _Alerts:
    def __init__(self):
        self.calls = []

    def alert(self, level, message, **ctx):
        self.calls.append((level, message))


@pytest.fixture
def world():
    return {}


# --- Scenario: thin credit is skipped before entry ---------------------------

@given('estimated net credit 2.00 at 95% (trigger 1.90) and the short put mid is 3.00')
def _(world):
    broker = FakeBroker()
    clock = FakeClock(datetime(2026, 7, 6, 10, 0, tzinfo=ET))
    svc = ExecuteEntryAttempt(broker, clock, [], SPX)
    condor = Condor(entry_number=2, put_short=D("5990"), call_short=D("6060"),
                    put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                    mid_credit=D("2.00"), min_total_credit=D("2.00"))
    world["outcome"] = asyncio.run(svc.attempt(
        day="2026-07-06", scheduled=clock.now(), condor=condor, gates=GATES_PASS))
    world["broker"] = broker


@then('the entry is SKIPPED with reason "infeasible_stop" and no order is submitted')
def _(world):
    assert world["outcome"].status == "SKIPPED" and world["outcome"].reason == "infeasible_stop"
    assert world["broker"]._orders == {}


# --- Scenario: healthy credit passes -----------------------------------------

@given('estimated net credit 4.00 (trigger 3.80) vs shorts at 3.00 and 2.00')
def _(world):
    world["feasible"] = feasible(
        StopBasis.TOTAL_CREDIT, ticks=SPX,
        short_prices={"PUT": D("3.00"), "CALL": D("2.00")},
        pct=D("95"), total_net_credit=D("4.00"), min_distance_ticks=2)
    broker, events = FakeBroker(), []
    clock = FakeClock(datetime(2026, 7, 6, 10, 0, tzinfo=ET))
    p = ProtectPosition(broker, clock, _Alerts(), events, SPX)
    shorts = [ShortLeg("PUT", D("3.00"), D("0.50")), ShortLeg("CALL", D("2.00"), D("0.50"))]
    world["result"] = asyncio.run(p.protect(entry_id="e", basis=StopBasis.TOTAL_CREDIT,
                                             shorts=shorts, total_net_credit=D("4.00")))
    world["broker"] = broker


@then('triggers clear both fills by the minimum distance and stops are placed')
def _(world):
    assert world["feasible"] is True
    assert world["result"].outcome == "PROTECTED"
    assert len(asyncio.run(world["broker"].working_orders())) == 2


# --- Scenario: post-fill infeasibility closes --------------------------------

@given("fills land such that the actual trigger does not clear a short's fill")
def _(world):
    broker, events = FakeBroker(), []
    clock = FakeClock(datetime(2026, 7, 6, 10, 0, tzinfo=ET))
    alerts = _Alerts()
    world["closed"] = []

    async def close_cb(entry_id, initiator):
        world["closed"].append((entry_id, initiator))

    p = ProtectPosition(broker, clock, alerts, events, SPX, close_entry=close_cb)
    # trigger 1.90 (net 2.00 @95) does not clear the 3.00 short fill
    shorts = [ShortLeg("PUT", D("3.00"), D("1.50")), ShortLeg("CALL", D("2.00"), D("1.50"))]
    world["result"] = asyncio.run(p.protect(entry_id="e5", basis=StopBasis.TOTAL_CREDIT,
                                             shorts=shorts, total_net_credit=D("2.00")))
    world["broker"], world["alerts"] = broker, alerts


@then('no stop is placed for that entry')
def _(world):
    assert asyncio.run(world["broker"].working_orders()) == []


@then('the entry closes via CLS-01 with initiator "infeasible_stop" and an alert')
def _(world):
    assert world["closed"] == [("e5", "infeasible_stop")]
    assert world["alerts"].calls  # an alert fired


# --- Scenario: markup counts toward feasibility ------------------------------

@given('a rebate markup that lifts the trigger above fill + minimum distance')
def _(world):
    # net 3.00 @95 -> 2.85 trigger vs a 3.00 short would be infeasible; a 0.50
    # markup lifts it to 3.35, clearing the 3.00 short by 2+ ticks
    base = feasible(StopBasis.TOTAL_CREDIT, ticks=SPX, short_prices={"PUT": D("3.00")},
                    pct=D("95"), total_net_credit=D("3.00"), min_distance_ticks=2)
    with_markup = feasible(StopBasis.TOTAL_CREDIT, ticks=SPX, short_prices={"PUT": D("3.00")},
                           pct=D("95"), markup=D("0.50"), total_net_credit=D("3.00"),
                           min_distance_ticks=2)
    world["base_feasible"], world["markup_feasible"] = base, with_markup


@then('the entry is feasible   # STP-02b adds to the trigger before the check')
def _(world):
    assert world["base_feasible"] is False   # without markup: infeasible
    assert world["markup_feasible"] is True  # markup lifts it over the line
