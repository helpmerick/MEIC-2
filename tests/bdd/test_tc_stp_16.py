"""Hand-written step definitions for TC-STP-16 — the seven pinned stop vectors
+ the 3.80-never-5.85 regression guard (STP-02 v1.38/v1.39).

Account-protection math: every number here is operator-ratified. The triggers
come from meic.domain.stop_policy; the P&L outcomes use the same contract as
the projection (credit − stop fills, ×100 per contract).
"""
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then

from meic.domain.stop_policy import StopBasis, clears, feasible, stop_trigger
from meic.domain.ticks import TickRung, TickTable

scenarios("../features/TC-STP-16.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


def _dollars(credit: D, trigger: D, sides_stopped: int) -> D:
    """Outcome-contract P&L in dollars: (credit − Σtriggers) × 100 per contract."""
    return (credit - trigger * sides_stopped) * 100


@pytest.fixture
def world():
    return {}


# --- Vector 1: canonical 400-dollar contract ---------------------------------

@given('shorts 3.00 + 2.00, wings 0.50 + 0.50, net credit 4.00, pct 95')
def _(world):
    world["credit"] = D("4.00")
    world["trigger"] = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"), total_net_credit=D("4.00"))


@then('both triggers = 3.80 exactly')
def _(world):
    assert world["trigger"] == D("3.80")


@then('one side stopped with the other expiring nets +20')
def _(world):
    assert _dollars(world["credit"], world["trigger"], 1) == D("20")


@then('both sides stopped nets -360')
def _(world):
    assert _dollars(world["credit"], world["trigger"], 2) == D("-360")


# --- Vector 2: pct 100 boundary ----------------------------------------------

@given('the same trade at pct 100')
def _(world):
    world["credit"] = D("4.00")
    world["trigger"] = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("100"), total_net_credit=D("4.00"))


@then('both triggers = 4.00, one-side nets 0, both-sides nets -400 exactly')
def _(world):
    assert world["trigger"] == D("4.00")
    assert _dollars(world["credit"], world["trigger"], 1) == D("0")
    assert _dollars(world["credit"], world["trigger"], 2) == D("-400")


# --- Vector 3: floor rounding, 0.10-tick regime ------------------------------

@given('net credit 3.60 at pct 95 (raw trigger 3.42)')
def _(world):
    world["trigger"] = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"), total_net_credit=D("3.60"))


@then('the trigger floors to 3.40, never 3.50')
def _(world):
    assert world["trigger"] == D("3.40")
    assert world["trigger"] != D("3.50")


# --- Vector 4: floor rounding, 0.05-tick regime ------------------------------

@given('net credit 3.10 at pct 95 (raw trigger 2.945)')
def _(world):
    world["trigger"] = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"), total_net_credit=D("3.10"))


@then('the trigger floors to 2.90, never 2.95')
def _(world):
    assert world["trigger"] == D("2.90")
    assert world["trigger"] != D("2.95")


# --- Vector 5: markup spends the one-side guarantee --------------------------

@given('vector 1 plus stop_rebate_markup 0.50')
def _(world):
    world["credit"] = D("4.00")
    world["trigger"] = stop_trigger(
        StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"), markup=D("0.50"), total_net_credit=D("4.00"))


@then('both triggers = 4.30')
def _(world):
    assert world["trigger"] == D("4.30")


@then('a one-side hit nets -30 plus long recovery   # the +20 guarantee is traded away by the dial')
def _(world):
    assert _dollars(world["credit"], world["trigger"], 1) == D("-30")


@then('both sides nets -460')
def _(world):
    assert _dollars(world["credit"], world["trigger"], 2) == D("-460")


# --- Vector 6: feasibility kill ----------------------------------------------

@given('shorts 3.00 + 2.00 with wings 1.50 + 1.50 (net credit 2.00, raw trigger 1.90)')
def _(world):
    world["trigger"] = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"), total_net_credit=D("2.00"))
    world["feasible"] = feasible(
        StopBasis.TOTAL_CREDIT, ticks=SPX,
        short_prices={"PUT": D("3.00"), "CALL": D("2.00")},
        pct=D("95"), total_net_credit=D("2.00"), min_distance_ticks=2)


@then('the trigger sits below the 3.00 short and the entry is SKIPPED "infeasible_stop"')
def _(world):
    assert world["trigger"] < D("3.00")
    assert world["feasible"] is False  # caller skips with reason infeasible_stop


# --- Vector 7: feasibility knife-edge ----------------------------------------

@given('net credit 3.37 at pct 95 (raw 3.2015, floors to 3.20) vs a 3.00 short')
def _(world):
    world["trigger"] = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"), total_net_credit=D("3.37"))
    world["clears"] = clears(world["trigger"], D("3.00"), ticks=SPX, min_distance_ticks=2)


@then('clearance is exactly 2 ticks and the entry is FEASIBLE   # rule is >=')
def _(world):
    assert world["trigger"] == D("3.20")
    assert (world["trigger"] - D("3.00")) == D("0.20")  # exactly 2 × 0.10 tick
    assert world["clears"] is True


# --- Regression guard: 3.80 never 5.85 ---------------------------------------

@given('vector 1 with stop_basis = total_credit')
def _(world):
    world["trigger"] = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"), total_net_credit=D("4.00"))


@then('the trigger MUST be 3.80 and MUST NOT be 5.85')
def _(world):
    # 5.85 = short_premium basis on the 3.00 short (3.00 × 1.95) — the retired
    # per-leg default. If it ever reappears here, the default crept back in.
    assert world["trigger"] == D("3.80"), "per-leg (short_premium) default has crept back in"
    assert world["trigger"] != D("5.85")
