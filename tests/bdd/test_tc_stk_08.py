"""Hand-written step definitions for TC-STK-08 — the eight probe-walk vectors
(STK-02 v1.39, unblocked by the v1.40 line-join amendment).

These bind the same vectors pinned at unit level in
tests/domain/test_chain_and_walk.py to the operator's Gherkin.
"""
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then

from meic.domain.chain import ChainSide, Mark
from meic.domain.walk import Selected, Skip, probe_prices, select_side

scenarios("../features/TC-STK-08.feature")

WALK = dict(target_premium=D("3.00"), wing_width=D("50"), otm_direction=D(-1))


def _side(mids: dict) -> ChainSide:
    """Put side: candidates plus cheap marked wings 50 below each."""
    full = dict(mids)
    for k in list(mids):
        full.setdefault(k - 50, "0.10")
    strikes = tuple(sorted((D(str(s)) for s in full), reverse=True))
    marks = {}
    for k, mid in full.items():
        m = D(str(mid))
        marks[D(str(k))] = Mark(bid=m - D("0.02"), ask=m + D("0.02"))
    return ChainSide(strikes, marks)


@pytest.fixture
def world():
    return {}


# --- Vector A -----------------------------------------------------------------

@given('strikes with raw mids 3.20, 2.93, 2.70   # 2.93 rounds to probe price 2.95')
def _(world):
    world["result"] = select_side(_side({6000: "3.20", 5995: "2.93", 5990: "2.70"}), **WALK)


@then('probes run 3.00 (miss), 2.95 (MATCH)')
def _(world):
    r = world["result"]
    assert isinstance(r, Selected) and r.probe_number == 2 and r.probe_price == D("2.95")


@then('the 2.93 strike is sold')
def _(world):
    assert world["result"].short_strike == D("5995") and world["result"].short_mid == D("2.93")


# --- Vector B -----------------------------------------------------------------

@given('strikes with raw mids 3.30, 3.05, 2.80')
def _(world):
    world["result"] = select_side(_side({6000: "3.30", 5995: "3.05", 5990: "2.80"}), **WALK)


@then('probes run 3.00, 2.95, 3.05 (MATCH)')
def _(world):
    r = world["result"]
    assert isinstance(r, Selected) and r.probe_number == 3 and r.probe_price == D("3.05")


@then('the 3.05 strike is sold')
def _(world):
    assert world["result"].short_strike == D("5995") and world["result"].short_mid == D("3.05")


# --- Vector C -----------------------------------------------------------------

@given('strikes with raw mids 3.45, 3.20, 2.80')
def _(world):
    world["side_c"] = _side({6000: "3.45", 5995: "3.20", 5990: "2.80"})
    world["result"] = select_side(world["side_c"], **WALK)


@then('all seven probes 3.00 to 3.15 miss')
def _(world):
    assert world["result"].probe_number > 7  # matched only in the down-only phase


@then('the down-only phase matches 2.80')
def _(world):
    r = world["result"]
    assert r.probe_price == D("2.80") and r.probe_number == 8  # first down-only probe


@then('the 3.20 strike is never selected despite equal distance to target')
def _(world):
    assert world["result"].short_strike != D("5995")  # the 3.20 strike
    assert world["result"].short_strike == D("5990")


# --- Vector D -----------------------------------------------------------------

@given("no strike's rounded mid lies between 1.75 and 3.15")
def _(world):
    world["result"] = select_side(_side({6000: "3.45", 5995: "1.60"}), **WALK)


@then('all 3 up-probes and all 25 down-probes miss')
def _(world):
    assert world["result"] == Skip("no_valid_strikes")


@then('the entry is SKIPPED with reason "no_valid_strikes"')
def _(world):
    assert world["result"] == Skip("no_valid_strikes")


# --- Vector E -----------------------------------------------------------------

@given('a strike with raw mid 1.80 and nothing nearer the 3.00 target')
def _(world):
    world["result"] = select_side(_side({5990: "1.80"}), **WALK)


@then('the down-only phase matches at probe 1.80 (within the 25-step depth)')
def _(world):
    r = world["result"]
    assert isinstance(r, Selected) and r.probe_price == D("1.80")


@then('the strike is sold   # 1.80 >= the 1.00 hard floor')
def _(world):
    assert world["result"].short_strike == D("5990")


# --- Vector E2 ------------------------------------------------------------------

@given('target 2.00 and the only match would be at raw mid 0.95')
def _(world):
    world["result"] = select_side(
        _side({5990: "0.95"}), target_premium=D("2.00"), wing_width=D("50"), otm_direction=D(-1))
    world["floor_probes"] = probe_prices(D("2.00"), floor=max(D("2.00") - D("1.25"), D("1.00")))


@then('the effective floor is max(2.00 - 1.25, 1.00) = 1.00')
def _(world):
    assert min(world["floor_probes"]) == D("1.00")


@then('probes below 1.00 are never taken')
def _(world):
    assert all(p >= D("1.00") for p in world["floor_probes"])


# --- Rounding lattice -----------------------------------------------------------

@given('a strike with raw mid 2.92')
def _(world):
    world["result"] = select_side(_side({5995: "2.92"}), **WALK)


@then('it answers probe 2.90, not 2.95   # 2.92 rounds down to 2.90')
def _(world):
    r = world["result"]
    assert isinstance(r, Selected) and r.probe_price == D("2.90") and r.probe_number == 4


# --- Deterministic, logged order -------------------------------------------------

@then('the exact sequence T, T-0.05, T+0.05, T-0.10, T+0.10, T-0.15, T+0.15, T-0.20, T-0.25 ... is enumerated verbatim')
def _():
    seq = probe_prices(D("3.00"), floor=D("1.75"))
    head = tuple(D(p) for p in ("3.00", "2.95", "3.05", "2.90", "3.10", "2.85", "3.15", "2.80", "2.75"))
    assert seq[: len(head)] == head
    assert seq[len(head):] == tuple(D("2.75") - D("0.05") * k for k in range(1, len(seq) - len(head) + 1))


@then('the day report records which probe number matched')
def _():
    r = select_side(_side({5995: "2.93"}), **WALK)
    assert isinstance(r, Selected) and isinstance(r.probe_number, int) and r.probe_number >= 1
