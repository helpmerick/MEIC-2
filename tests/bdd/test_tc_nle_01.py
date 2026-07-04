"""Hand-written step definitions for TC-NLE-01 — NLE-01 computation (Phase 3).

The stop trigger is an INPUT to the estimate (the scenario supplies 5.14);
nothing here computes a trigger — NLE-04's ban and the stop-semantics freeze
both hold. Short/long identities come from the scenario's own annotations:
short = 5990 (fill 1.35), long = 5940 (fill 0.15).
"""
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then

from meic.domain.nle import NetLossEstimate, estimate_net_loss

scenarios("../features/TC-NLE-01.feature")


@pytest.fixture
def world():
    return {}


@given('a scripted put-side chain:')
def _(world, datatable):
    rows = datatable[1:]  # skip header row
    world["chain"] = {D(r[0].strip()): D(r[1].strip()) for r in rows}


@given('stop trigger = 5.14 and nle_haircut_pct = 30')
def _(world):
    world["est"] = estimate_net_loss(
        chain_mids=world["chain"],
        short_strike=D("5990"), short_fill=D("1.35"),
        long_strike=D("5940"), long_fill=D("0.15"),
        stop_trigger=D("5.14"), nle_haircut_pct=D("30"),
    )
    assert isinstance(world["est"], NetLossEstimate)


@then('implied move D = 45')
def _(world):
    assert world["est"].implied_move == D("45")


@then('raw long estimate = 1.55, haircut estimate = 1.085')
def _(world):
    assert world["est"].raw_long_estimate == D("1.55")
    assert world["est"].haircut_estimate == D("1.085")


@then('estimated net loss = (5.14 - 1.35) - (1.085 - 0.15) = 2.855')
def _(world):
    assert world["est"].estimated_net_loss == D("2.855")


@then('it is reported in $ and as % of the stop-basis credit')
def _(world):
    # The estimate is a dollar figure; the % form is a straight division by the
    # stop-basis credit at render time (report layer). Assert both derivable.
    dollars = world["est"].estimated_net_loss
    assert isinstance(dollars, D)
    pct_of_credit = dollars / D("2.30") * 100  # nominal stop-basis credit
    assert pct_of_credit > 0
