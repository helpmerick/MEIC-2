"""Hand-written step definitions for TC-STK-02 — STK-02/02a/03/05/06 (Phase 3).

The walk and gate scenarios are real implementations. The final scenario
('Stops and P&L use net fill credit...') encodes stop semantics and stays
FROZEN until the STP-05a findings are reviewed and the Phase 2 PR is merged
(operator direction, 2026-07-04).
"""
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then

from meic.domain.chain import ChainSide, Mark
from meic.domain.gates import GatesFailed, GatesPassed, check_credit_gates
from meic.domain.walk import Selected, Skip, select_side

scenarios("../features/TC-STK-02.feature")

WALK = dict(target_premium=D("3.00"), tolerance=D("0.10"), wing_width=D("50"), otm_direction=D(-1))
FLOORS = dict(min_short_premium=D("1.00"), min_total_credit=D("2.00"))


def _mk(mid: str) -> Mark:
    m = D(mid)
    return Mark(bid=m - D("0.05"), ask=m + D("0.05"))


def _walk_adjacent(world, first_mid: str, second_mid: str) -> None:
    marks = {D("6000"): _mk(first_mid), D("5995"): _mk(second_mid),
             D("5950"): _mk("0.60"), D("5945"): _mk("0.55")}
    side = ChainSide((D("6000"), D("5995"), D("5950"), D("5945")), marks)
    world["result"] = select_side(side, **WALK)
    world["mid_of"] = {D("6000"): D(first_mid), D("5995"): D(second_mid)}


@pytest.fixture
def world():
    return {}


@given('target_premium = 3.00, target_premium_tolerance = 0.10')
def _(world):
    pass  # constants live in WALK; ceiling = 3.10


@given('adjacent put strikes with mids 3.10 and 2.85')
def _(world):
    _walk_adjacent(world, "3.10", "2.85")


@given('adjacent put strikes with mids 3.11 and 2.85')
def _(world):
    _walk_adjacent(world, "3.11", "2.85")


@given('adjacent put strikes with mids 3.25 and 2.85')
def _(world):
    _walk_adjacent(world, "3.25", "2.85")


@given('every strike with valid quotes has mid > 3.10')
def _(world):
    side = ChainSide((D("6000"), D("5995")), {D("6000"): _mk("3.50"), D("5995"): _mk("3.30")})
    world["result"] = select_side(side, **WALK)


@then('the short put strike is the 3.10 strike   # ceiling = 3.10 inclusive; richest qualifying wins')
def _(world):
    r = world["result"]
    assert isinstance(r, Selected) and world["mid_of"][r.short_strike] == D("3.10")


@then('the short put strike is the 2.85 strike   # 3.11 > 3.10 ceiling')
def _(world):
    r = world["result"]
    assert isinstance(r, Selected) and world["mid_of"][r.short_strike] == D("2.85")


@then('the short put strike is the 2.85 strike   # 3.25 above ceiling even though closer to target')
def _(world):
    r = world["result"]
    assert isinstance(r, Selected) and world["mid_of"][r.short_strike] == D("2.85")


@then('the long put strike = short - wing_width   # STK-03, regardless of its cost')
def _(world):
    r = world["result"]
    assert r.long_strike == r.short_strike - D("50")


@then('the entry is SKIPPED with reason "no_valid_strikes"')
def _(world):
    assert world["result"] == Skip("no_valid_strikes")


# --- Same short target, expensive wing => net credit gate (STK-06) -----------

@given('target_premium = 3.00 and both shorts fill their premium floor')
def _(world):
    world["shorts"] = (D("2.95"), D("2.95"))  # both comfortably >= 1.00


@given('in the morning the wings cost 1.00 each (total net = 3.90)')
def _(world):
    put, call = world["shorts"]
    world["morning"] = check_credit_gates(
        put_short_mid=put, call_short_mid=call, total_net_credit_mid=D("3.90"), **FLOORS)


@given('at the 12:30 entry the wings cost 2.10 each (total net = 1.90)')
def _(world):
    put, call = world["shorts"]
    world["midday"] = check_credit_gates(
        put_short_mid=put, call_short_mid=call, total_net_credit_mid=D("1.90"), **FLOORS)


@then('the morning entry proceeds')
def _(world):
    assert isinstance(world["morning"], GatesPassed)


@then('the 12:30 entry is SKIPPED with reason "insufficient_credit"  # STK-06: total NET < 2.00 aborts')
def _(world):
    assert world["midday"] == GatesFailed("insufficient_credit")


# --- Thin side trades when the total floor passes (accepted by design) -------

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


# --- FROZEN: stop semantics (operator direction 2026-07-04) ------------------

@given('the condor fills with short put 3.00 and long put 1.00')
def _():
    raise NotImplementedError(
        "TC-STK-02: per_side stop math — STOP SEMANTICS FROZEN until the "
        "STP-05a findings report is reviewed and the Phase 2 PR is merged")


@then('per_side stop math uses side net credit 2.00 (not 3.00)')
def _():
    raise NotImplementedError("TC-STK-02: stop semantics frozen (see above)")


@then('the day report shows short premium and net credit as separate labelled figures  # UI-14')
def _():
    raise NotImplementedError("TC-STK-02: reporting/UI phase, and stop semantics frozen")
