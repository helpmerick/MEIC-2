"""Hand-written step definitions for TC-STK-07 — STK-10/11 chain integrity (Phase 3).

Domain-pure substance is real: gate outcomes, adjacency rejections, and the
heal-then-proceed progression are modeled as before/after chain snapshots.
The retry CADENCE (chain_retry_seconds timer, entry-window expiry clock) is
application-layer and gets exercised by the application-phase tests; the
skip reasons and gate verdicts asserted here are the domain truth they reuse.
"""
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.domain.chain import ChainSide, Mark, adjacency_ok, completeness_ok
from meic.domain.walk import Selected, Skip, WingUnmarked, select_side

scenarios("../features/TC-STK-07.feature")

WALK = dict(target_premium=D("3.00"), tolerance=D("0.10"), wing_width=D("50"), otm_direction=D(-1))


def _mk(mid: str) -> Mark:
    m = D(mid)
    return Mark(bid=m - D("0.05"), ask=m + D("0.05"))


BAND = tuple(D(str(s)) for s in (6000, 5995, 5990, 5985))


@pytest.fixture
def world():
    return {}


# --- Scenario: holey chain blocks, heals, proceeds ---------------------------

@given('only 75% of strikes within the ATM band have marks at fire time')
def _(world):
    marks_75 = {D("6000"): _mk("3.05"), D("5995"): _mk("2.85"), D("5990"): _mk("2.60")}
    world["side_t0"] = ChainSide(BAND + (D("5950"), D("5945")), marks_75)


@then('no strike selection occurs and the gate retries every chain_retry_seconds')
def _(world):
    # Gate verdict blocks selection; the retry timer itself is application-layer.
    assert not completeness_ok(world["side_t0"], band_strikes=BAND, completeness_pct=D("90"))


@when('the chain completes at T+20s (within the entry window)')
def _(world):
    marks_full = {D("6000"): _mk("3.05"), D("5995"): _mk("2.85"), D("5990"): _mk("2.60"),
                  D("5985"): _mk("2.40"), D("5950"): _mk("0.60"), D("5945"): _mk("0.55")}
    world["side_t1"] = ChainSide(BAND + (D("5950"), D("5945")), marks_full)


@then('selection proceeds normally')
def _(world):
    assert completeness_ok(world["side_t1"], band_strikes=BAND, completeness_pct=D("90"))
    assert isinstance(select_side(world["side_t1"], **WALK), Selected)


# --- Scenario: persistent holes -> skip -------------------------------------

@given('the chain never reaches chain_completeness_pct within entry_window_seconds')
def _(world):
    marks_75 = {D("6000"): _mk("3.05"), D("5995"): _mk("2.85"), D("5990"): _mk("2.60")}
    side = ChainSide(BAND, marks_75)
    # window expiry is the application clock; the domain names the skip reason
    world["skip"] = None if completeness_ok(side, band_strikes=BAND, completeness_pct=D("90")) else "incomplete_chain"


@then('the entry is SKIPPED with reason "incomplete_chain" and no order is submitted')
def _(world):
    assert world["skip"] == "incomplete_chain"


# --- Scenario: adjacency catches a hole at the target ------------------------

@given('the completeness gate passes at 90%')
def _(world):
    world["completeness_passed"] = True  # context; the hole sits AT the target


@given("the strike one step closer to the money than the walk's selection has no mark")
def _(world):
    # 6000 is a hole; 5995 marked at 2.85 — walk lands on 5995, guard rejects
    side = ChainSide((D("6000"), D("5995"), D("5945")),
                     {D("5995"): _mk("2.85"), D("5945"): _mk("0.55")})
    world["result"] = select_side(side, **WALK)


@then('the selection is rejected and treated as a STK-10 failure (retry, then skip)')
def _(world):
    assert world["result"] == Skip("incomplete_chain")


# --- Scenario: adjacency catches a leapt hole --------------------------------

@given('the strike one step closer to the money is marked BELOW the ceiling')
def _(world):
    # A richer qualifying strike exists one step closer: continuity is disproven
    side = ChainSide((D("6000"), D("5995")), {D("6000"): _mk("3.02"), D("5995"): _mk("2.85")})
    world["adjacency"] = adjacency_ok(side, D("5995"), ceiling=D("3.10"))


@then('the selection is rejected            # the walk should have selected that richer strike')
def _(world):
    assert world["adjacency"] is False


# --- Scenario: missing wing retries ------------------------------------------

@given('the wing strike has no mark at fire time but appears at T+15s')
def _(world):
    strikes = (D("6000"), D("5995"), D("5950"))
    t0 = ChainSide(strikes, {D("6000"): _mk("3.05"), D("5995"): _mk("2.85")})
    t1 = ChainSide(strikes, {D("6000"): _mk("3.05"), D("5995"): _mk("2.85"), D("5950"): _mk("0.60")})
    world["r_t0"] = select_side(t0, **WALK)
    world["r_t1"] = select_side(t1, **WALK)


@then('the entry proceeds with the correct wing (no guessing, no immediate skip)')
def _(world):
    assert isinstance(world["r_t0"], WingUnmarked)  # retry condition, NOT a skip
    r = world["r_t1"]
    assert isinstance(r, Selected) and r.long_strike == D("5950")


# --- Scenario: far-OTM emptiness ---------------------------------------------

@given('strikes outside the ATM band have no bids')
def _(world):
    side = ChainSide(BAND + (D("5000"), D("4900")),
                     {D("6000"): _mk("3.05"), D("5995"): _mk("2.85"),
                      D("5990"): _mk("2.60"), D("5985"): _mk("2.40")})
    world["far_otm_ok"] = completeness_ok(side, band_strikes=BAND, completeness_pct=D("90"))


@then('the chain-integrity gate still passes')
def _(world):
    assert world["far_otm_ok"] is True
