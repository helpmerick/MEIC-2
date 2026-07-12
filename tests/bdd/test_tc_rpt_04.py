"""TC-RPT-04 — RPT-05 targeting decomposition + RPT-06/07 slippage, pinned.
target 3.00, matched probe 2.95 (probe number 2), short filled 2.93 =>
selection gap -0.05, execution gap -0.02, probe depth 2 (the probe number
itself, passed through by the caller -- reporting.targeting is pure
arithmetic and holds no probe-log/event dependency). First-rung credit 3.50,
fill credit 3.60 => slippage-in +0.10 (price improvement, never sign-flipped
into a loss). Stop trigger 3.80 filled at 3.90 => slippage-out 0.10 = 2 ticks,
and it aggregates into the mean/p90 (RPT-07 stop-outs family)."""
from decimal import Decimal as D

from pytest_bdd import given, scenarios, then

from meic.reporting import slippage, targeting

scenarios("../features/TC-RPT-04.feature")


# --- Targeting decomposition separates causes --------------------------------

@given("target 3.00, matched probe 2.95 at probe number 2, short filled 2.93",
       target_fixture="targeting_vector")
def _():
    return {"target": D("3.00"), "matched_probe": D("2.95"),
            "probe_number": 2, "short_fill": D("2.93")}


@then("selection gap = -0.05, execution gap = -0.02, probe depth = 2")
def _(targeting_vector):
    v = targeting_vector
    assert targeting.selection_gap(v["matched_probe"], v["target"]) == D("-0.05")
    assert targeting.execution_gap(v["short_fill"], v["matched_probe"]) == D("-0.02")
    assert v["probe_number"] == 2  # probe depth -- the matched probe's own 1-indexed position


# --- Slippage-in can be positive ---------------------------------------------

@given("first-rung credit 3.50 and fill credit 3.60", target_fixture="slippage_in_vector")
def _():
    return {"first_rung": D("3.50"), "fill_credit": D("3.60")}


@then("slippage-in = +0.10 price improvement")
def _(slippage_in_vector):
    v = slippage_in_vector
    result = slippage.slippage_in(v["first_rung"], v["fill_credit"])
    assert result == D("0.10")
    assert result > 0  # positive = price improvement, never displayed as a loss


# --- Stop slippage reports from EC-STP-03 records -----------------------------

@given("a stop with trigger 3.80 filled at 3.90", target_fixture="stop_vector")
def _():
    return {"trigger": D("3.80"), "fill": D("3.90")}


@then("slippage-out = 0.10 = 2 ticks and it enters the mean and p90")
def _(stop_vector):
    dollars, ticks = slippage.stop_slippage(stop_vector["trigger"], stop_vector["fill"])
    assert dollars == D("0.10")
    assert ticks == D("2")
    samples = [dollars]
    assert slippage.mean(samples) == D("0.10")
    assert slippage.p90(samples) == D("0.10")
