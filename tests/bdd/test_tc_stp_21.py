"""Hand-written step definitions for TC-STP-21 — STP-02b effective-percentage
cage (v1.67): a fixed-dollar `stop_rebate_markup` bites harder, as a
percentage, the smaller the credit. The trigger stays in dollars (unchanged
STP-02/02b math); the effective percentage is DISPLAYED and CAGED against
`max_effective_stop_pct` (default 110) -- reject-never-clamp.
"""
import asyncio
from datetime import datetime
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then

from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.domain.stop_policy import (
    StopBasis,
    effective_cap_check,
    effective_stop_pct,
    stop_trigger,
    within_effective_cap,
)
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import ET, FakeClock

scenarios("../features/TC-STP-21.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
GATES_PASS = GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                          flatten_in_progress=False, market_open=True, market_halted=False,
                          data_fresh=True, session_valid=True, buying_power_ok=True)


@pytest.fixture
def world():
    return {}


@given("credit 2.80 and markup 0.30 with max_effective_stop_pct = 110")
def _(world):
    world["credit"], world["markup"], world["cap"] = D("2.80"), D("0.30"), D("110")


@then("the trigger floors to 2.95 and the display shows effective 105.4 percent — allowed")
def _(world):
    trigger = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"),
                           markup=world["markup"], total_net_credit=world["credit"])
    assert trigger == D("2.95")   # raw 0.95*2.80+0.30 = 2.96 floors DOWN to 2.95 (STP-02 v1.39)
    eff = effective_stop_pct(trigger, world["credit"])
    assert eff == D("105.4")      # NOT the raw pre-floor 105.7% -- the precision trap TC-STP-21 pins
    assert within_effective_cap(eff, world["cap"]) is True


@then("on credit 2.00 the same markup shows 110 percent — allowed at the boundary")
def _(world):
    credit2 = D("2.00")
    trigger2 = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"),
                            markup=world["markup"], total_net_credit=credit2)
    eff2 = effective_stop_pct(trigger2, credit2)
    assert eff2 == D("110.0")
    # 110 exactly is the ALLOWED boundary — the cap never rejects its own edge.
    assert within_effective_cap(eff2, world["cap"]) is True


@then('a combination exceeding the cap skips with reason "markup_exceeds_cap", never clamped')
def _(world):
    # credit 1.90, markup 0.30, pct 95: raw 0.95*1.90+0.30 = 2.105 floors to
    # 2.10 -> effective 110.526...% -> 110.5%, over the 110 cap.
    over_credit = D("1.90")
    ok, worst = effective_cap_check(
        StopBasis.TOTAL_CREDIT, ticks=SPX,
        short_prices={"PUT": D("0.50"), "CALL": D("0.50")},
        net_credit=over_credit, pct=D("95"), markup=world["markup"], cap_pct=world["cap"])
    assert worst == D("110.5")
    assert ok is False

    # NEVER CLAMPED: a markup silently reduced to fit the 110% cap would need
    # ~0.285, not 0.30 -- a materially smaller trigger. Confirm the trigger the
    # cage actually computed used the operator's FULL, unmodified 0.30.
    trigger = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"),
                           markup=world["markup"], total_net_credit=over_credit)
    assert trigger == D("2.10")

    # And prove the skip is real, end to end, through the entry pipeline --
    # not just the domain predicate: ExecuteEntryAttempt.attempt() records
    # EntrySkipped(reason="markup_exceeds_cap") and no order is ever submitted.
    broker = FakeBroker()
    clock = FakeClock(datetime(2026, 7, 6, 10, 0, tzinfo=ET))
    events = []
    svc = ExecuteEntryAttempt(broker, clock, events, SPX,
                              stop_loss_pct=D("95"), stop_rebate_markup=world["markup"],
                              max_effective_stop_pct=world["cap"])
    condor = Condor(entry_number=3, put_short=D("5990"), call_short=D("6060"),
                    put_short_mid=D("0.50"), call_short_mid=D("0.50"),
                    mid_credit=over_credit, min_total_credit=D("1.00"))
    outcome = asyncio.run(svc.attempt(day="2026-07-06", scheduled=clock.now(),
                                      condor=condor, gates=GATES_PASS))
    assert outcome.status == "SKIPPED" and outcome.reason == "markup_exceeds_cap"
    assert broker._orders == {}
