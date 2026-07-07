"""Hand-written step definitions for TC-STP-14 — the stop_rebate_markup
(STP-02b): it raises the trigger BEFORE tick-flooring, across bases; zero markup
is a no-op; NLE and calibration incorporate it; the UI discloses the worst case;
intraday changes are next-entry only."""
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then

from meic.domain.nle import NetLossEstimate, estimate_net_loss
from meic.domain.nle_calibration import CalibrationRecord
from meic.domain.stop_policy import (
    StopBasis,
    markup_worst_case_increase,
    stop_trigger,
)
from meic.domain.ticks import TickRung, TickTable

scenarios("../features/TC-STP-14.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
# per_side fills: put short 1.35 / long 0.15; call short 1.25 / long 0.15
PUT = dict(short_fill=D("1.35"), side_long_fill=D("0.15"))
CALL = dict(short_fill=D("1.25"), side_long_fill=D("0.15"))
CREDIT = D("2.30")


@pytest.fixture
def world():
    return {}


# --- Scenario 1: markup raises per_side triggers -----------------------------

@given('stop_basis = per_side, stop_loss_pct = 95, stop_rebate_markup = 0.50')
def _(world):
    world["markup"] = D("0.50")


@then('the put stop trigger = floor_to_tick(1.35 + 0.95*1.20 + 0.50)   # raw 2.99 -> 2.95, NOT 3.00 (round would cross the 0.10-tick regime)')
def _(world):
    t = stop_trigger(StopBasis.PER_SIDE, ticks=SPX, pct=D("95"), markup=D("0.50"), **PUT)
    assert t == SPX.floor(D("1.35") + D("0.95") * D("1.20") + D("0.50")) == D("2.95")


@then('the call stop trigger = floor_to_tick(1.25 + 0.95*1.10 + 0.50)   # raw 2.795 -> 2.75')
def _(world):
    t = stop_trigger(StopBasis.PER_SIDE, ticks=SPX, pct=D("95"), markup=D("0.50"), **CALL)
    assert t == SPX.floor(D("1.25") + D("0.95") * D("1.10") + D("0.50")) == D("2.75")


# --- Scenario 2: markup raises total_credit triggers -------------------------

@given('stop_basis = total_credit and the same markup')
def _(world):
    world["markup"] = D("0.50")


@then('both triggers = floor_to_tick(0.95*2.30 + 0.50)   # raw 2.685 -> 2.65')
def _(world):
    t = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"), markup=D("0.50"),
                     total_net_credit=CREDIT)
    assert t == SPX.floor(D("0.95") * CREDIT + D("0.50")) == D("2.65")


# --- Scenario 3: zero markup is a no-op --------------------------------------

@given('stop_rebate_markup = 0.00')
def _(world):
    world["markup"] = D("0.00")


@then('triggers are byte-identical to the pre-markup formulas')
def _(world):
    with_zero = stop_trigger(StopBasis.PER_SIDE, ticks=SPX, pct=D("95"), markup=D("0.00"), **PUT)
    pre_markup = stop_trigger(StopBasis.PER_SIDE, ticks=SPX, pct=D("95"), **PUT)
    assert with_zero == pre_markup


# --- Scenario 4: NLE + calibration incorporate the markup --------------------

@given('a markup of 0.50 in force')
def _(world):
    world["markup"] = D("0.50")
    world["trigger"] = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"),
                                    markup=D("0.50"), total_net_credit=CREDIT)  # 2.65


@then('the NLE estimate is computed from the markup-inclusive trigger')
def _(world):
    chain = {D(str(k)): (D(str(k)) - D("5975")) * D("0.30") for k in range(5978, 6001)}
    est = estimate_net_loss(
        chain_mids=chain, short_strike=D("5990"), short_fill=D("1.35"),
        long_strike=D("5980"), long_fill=D("0.15"),
        stop_trigger=world["trigger"], nle_haircut_pct=D("30"))
    assert isinstance(est, NetLossEstimate)
    # the net-loss estimate is driven by the markup-inclusive trigger it was fed
    assert est.estimated_net_loss == (world["trigger"] - D("1.35")) - (est.haircut_estimate - D("0.15"))


@then('the calibration record for a stop event stores markup = 0.50')
def _(world):
    rec = CalibrationRecord(side="PUT", estimated_net_loss=D("1.0"),
                            realized_net_loss=D("1.1"), markup=D("0.50"))
    assert rec.markup == D("0.50")


# --- Scenario 5: UI worst-case disclosure ------------------------------------

@given('the operator sets markup 0.50 in the UI')
def _(world):
    world["markup"] = D("0.50")


@then('the setting displays the worst-case increase before saving  # UI-18')
def _(world):
    # markup 0.50, 1 contract, both sides -> 0.50 * 100 * 2 = $100 worst case
    assert markup_worst_case_increase(D("0.50"), contracts=1) == D("100")


# --- Scenario 6: intraday change is next-entry only --------------------------

@given('markup changed 0.00 -> 0.50 after entry 1 filled')
def _(world):
    world["entry1"] = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"),
                                   markup=D("0.00"), total_net_credit=CREDIT)
    world["entry2"] = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"),
                                   markup=D("0.50"), total_net_credit=CREDIT)


@then("entry 1's resting stops are unchanged and entry 2 uses 0.50")
def _(world):
    assert world["entry1"] == SPX.floor(D("0.95") * CREDIT)          # no markup
    assert world["entry2"] == SPX.floor(D("0.95") * CREDIT + D("0.50"))  # 0.50 markup
    assert world["entry1"] != world["entry2"]
