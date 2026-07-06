"""Hand-written step definitions for TC-STK-02 — STK-02a/03/05/06 (v1.39 rebase).

The feature was rebased onto the probe walk in v1.39; boundary vectors moved
to TC-STK-08. The wing/gate scenarios are real; the final scenario ('Stops
and P&L use net fill credit...') encodes stop semantics and stays FROZEN
until the STP-05a findings are reviewed and the Phase 2 PR is merged.
"""
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then

from meic.domain.chain import ChainSide, Mark
from meic.domain.gates import GatesFailed, GatesPassed, check_credit_gates
from meic.domain.walk import Selected, select_side

scenarios("../features/TC-STK-02.feature")

FLOORS = dict(min_short_premium=D("1.00"), min_total_credit=D("2.00"))


@pytest.fixture
def world():
    return {}


@given('the probe walk matches a short strike')
def _(world):
    mid = D("2.95")
    side = ChainSide(
        (D("6000"), D("5995"), D("5950"), D("5945")),
        {D("6000"): Mark(bid=mid - D("0.02"), ask=mid + D("0.02")),
         D("5950"): Mark(bid=D("0.55"), ask=D("0.65"))},  # wing costs what it costs
    )
    world["result"] = select_side(
        side, target_premium=D("3.00"), wing_width=D("50"), otm_direction=D(-1))
    assert isinstance(world["result"], Selected)


@then('the long wing is placed at wing_width regardless of its own cost  # STK-03')
def _(world):
    r = world["result"]
    assert r.long_strike == r.short_strike - D("50")


# --- Expensive wing => total NET floor (STK-06) -------------------------------

@given('both shorts match their probes and wings cost 2.10 each (total net = 1.90)')
def _(world):
    world["midday"] = check_credit_gates(
        put_short_mid=D("2.95"), call_short_mid=D("2.95"),
        total_net_credit_mid=D("1.90"), **FLOORS)


@then('the entry is SKIPPED with reason "insufficient_credit"  # STK-06: total NET < 2.00 aborts')
def _(world):
    assert world["midday"] == GatesFailed("insufficient_credit")


# --- Thin side trades when the total floor passes (accepted by design) --------

@given('the put side nets 0.10 after an expensive wing and the call side nets 2.20')
def _(world):
    world["thin_total"] = D("2.30")


@given('both shorts collected >= min_short_premium')
def _(world):
    world["thin"] = check_credit_gates(
        put_short_mid=D("1.10"), call_short_mid=D("2.30"),
        total_net_credit_mid=world["thin_total"], **FLOORS)


@then('the entry proceeds (total net 2.30 >= 2.00)   # per-side NET floor deliberately does not exist')
def _(world):
    assert isinstance(world["thin"], GatesPassed) and world["thin"].total_net_credit == D("2.30")


# --- FROZEN: stop semantics (operator direction 2026-07-04) -------------------

@given('the condor fills with short put 3.00 and long put 1.00')
def _():
    raise NotImplementedError(
        "TC-STK-02: stop math — STOP SEMANTICS FROZEN until the STP-05a "
        "findings report is reviewed and the Phase 2 PR is merged")


@then('stop math uses the actual net credit (not 3.00)')
def _():
    raise NotImplementedError("TC-STK-02: stop semantics frozen (see above)")


@then('the day report shows short premium and net credit as separate labelled figures  # UI-14')
def _():
    raise NotImplementedError("TC-STK-02: reporting/UI phase, and stop semantics frozen")
