"""Hand-written step definitions for TC-STK-07 — STK-10 chain integrity (Phase 3).

v1.39 NOTE: scenarios 3–4 of this feature still encode the RETIRED v1.4
adjacency guard ('one step closer to the money' rejections). v1.39's STK-11
replaced that guard with probe-match integrity — under the probe walk these
two scenarios describe behavior that no longer exists. A spec amendment to
rewrite/remove them has been proposed to the operator; their steps raise
NotImplementedError until ratified. Scenarios 1, 2, 5 and 6 remain real.

The retry CADENCE (chain_retry_seconds timer, entry-window expiry clock) is
application-layer; the gate verdicts and skip reasons asserted here are the
domain truth those loops reuse.
"""
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.domain.chain import ChainSide, Mark, completeness_ok
from meic.domain.walk import Selected, WingUnmarked, select_side

scenarios("../features/TC-STK-07.feature")

WALK = dict(target_premium=D("3.00"), wing_width=D("50"), otm_direction=D(-1))


def _mk(mid: str) -> Mark:
    m = D(mid)
    return Mark(bid=m - D("0.02"), ask=m + D("0.02"))


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


# --- Scenarios 3-4: RETIRED adjacency guard — blocked on proposed amendment ---

_ADJACENCY_STALE = (
    "TC-STK-07 scenarios 3-4 encode the v1.4 adjacency guard, retired by "
    "v1.39 STK-11 (probe-match integrity). Spec amendment proposed to the "
    "operator to rewrite/remove them; frozen until ratified."
)


@given('the completeness gate passes at 90%')
def _(world):
    pass  # context only; the stale adjacency step below blocks the scenario


@given("the strike one step closer to the money than the walk's selection has no mark")
def _():
    raise NotImplementedError(_ADJACENCY_STALE)


@then('the selection is rejected and treated as a STK-10 failure (retry, then skip)')
def _():
    raise NotImplementedError(_ADJACENCY_STALE)


@given('the strike one step closer to the money is marked BELOW the ceiling')
def _():
    raise NotImplementedError(_ADJACENCY_STALE)


@then('the selection is rejected            # the walk should have selected that richer strike')
def _():
    raise NotImplementedError(_ADJACENCY_STALE)


# --- Scenario: missing wing retries ------------------------------------------

@given('the wing strike has no mark at fire time but appears at T+15s')
def _(world):
    strikes = (D("6000"), D("5995"), D("5950"))
    t0 = ChainSide(strikes, {D("6000"): _mk("3.05")})
    t1 = ChainSide(strikes, {D("6000"): _mk("3.05"), D("5950"): _mk("0.60")})
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
