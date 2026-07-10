"""TC-RPT-03 — RPT-03 outcome taxonomy & the v1.38 contract audit. Both
scenarios bind cleanly at the pure-taxonomy layer built in slice 1."""
from decimal import Decimal as D

from pytest_bdd import given, scenario, then

from meic.domain.events import FilledLeg
from meic.domain.projection import EntryProjection
from meic.reporting.taxonomy import BOTH_SIDES_STOPPED, ONE_SIDE_STOPPED, classify, contract_audit

PCT_95 = D("0.95")
_LEG = FilledLeg(symbol="SPXW  260709P05600000", right="P", role="short", qty=1, price=D("3.00"))


@scenario("../features/TC-RPT-03.feature", "Outcomes classify exactly once and honor the v1.38 contract")
def test_outcomes_classify_exactly_once():
    pass


@scenario("../features/TC-RPT-03.feature", "A contract breach flags red")
def test_a_contract_breach_flags_red():
    pass


@given("the 4.00-credit canonical trade stopped on the put side only", target_fixture="one_side")
def _():
    # STP-02 total_credit @ 95%: trigger = 3.80; one side stops at exactly the
    # trigger (no slippage) -> realized = 4.00 - 3.80 = +0.20/share = +$20.
    return EntryProjection(
        entry_id="2026-07-09#1", net_credit=D("4.00"), stop_fills=D("3.80"),
        sides_stopped=("PUT",), stop_initiators=("resting_stop",), legs=(_LEG,))


@then("the entry is ONE_SIDE_STOPPED with realized >= +20 dollars minus recorded slippage")
def _(one_side):
    assert classify(one_side) == ONE_SIDE_STOPPED
    breach = contract_audit(one_side, pct=PCT_95, slippage_allowance=D("0"))
    assert breach is None  # exactly at the +$20 floor -- the contract holds, no breach


@then("a both-sides day classifies BOTH_SIDES_STOPPED with realized >= -360 dollars minus recorded slippage")
def _():
    # A separate entry (this scenario's Gherkin gives no second `Given` for
    # it, so it is built inline): both shorts stop at their 95% triggers ->
    # realized = 4.00 - 7.60 = -3.60/share = -$360.
    both_sides = EntryProjection(
        entry_id="2026-07-09#2", net_credit=D("4.00"), stop_fills=D("3.80") + D("3.80"),
        sides_stopped=("PUT", "CALL"), stop_initiators=("resting_stop", "resting_stop"),
        legs=(_LEG,))
    assert classify(both_sides) == BOTH_SIDES_STOPPED
    breach = contract_audit(both_sides, pct=PCT_95, slippage_allowance=D("0"))
    assert breach is None  # exactly at the -$360 floor -- the contract holds, no breach


@given("a ONE_SIDE_STOPPED entry whose realized loss exceeds the recorded slippage allowance",
       target_fixture="breaching_entry")
def _():
    # Pathological fill: the stop paid MORE than the credit collected on that
    # leg alone -- realized = 4.00 - 4.05 = -0.05/share = -$5, far below even
    # a generous $10 slippage allowance against the +$20 contractual floor.
    return EntryProjection(
        entry_id="2026-07-09#3", net_credit=D("4.00"), stop_fills=D("4.05"),
        sides_stopped=("PUT",), stop_initiators=("resting_stop",), legs=(_LEG,))


@then("the dashboard renders a contract-breach flag with a drill-down to its fills")
def _(breaching_entry):
    breach = contract_audit(breaching_entry, pct=PCT_95, slippage_allowance=D("10"))
    assert breach is not None
    assert breach.outcome == ONE_SIDE_STOPPED
    assert breach.realized == D("-5")   # -0.05/share * 100 * 1 contract
    # The drill-down key slice 1 hands the (later) UI layer -- the actual
    # fills drill-down view itself is deferred (needs the endpoints/UI slice).
    assert breach.entry_id == "2026-07-09#3"
