"""Hand-written step definitions for TC-TPF-02 — two-layer TPF validation (Phase 3).

The MATH both layers share (is_armable: reject, never clamp) is asserted here.
The UI presentation mechanics (grey-out in place, selector not reopening, UI
refresh) are re-verified by the TC-UI-* suite in the frontend phase — this
module asserts the validation verdicts that drive them.
"""
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.domain.tpf import is_armable, valid_levels

scenarios("../features/TC-TPF-02.feature")


@pytest.fixture
def world():
    return {}


@given('the selector is open with profit 25% and level 20 enabled')
def _(world):
    world["profit"] = D("25")
    assert is_armable(20, world["profit"])  # enabled at open


@when('streamed profit falls to 24%')
def _(world):
    world["profit"] = D("24")


@then('level 20 greys out in place without reopening the selector  # 24 - 20 < 5')
def _(world):
    assert not is_armable(20, world["profit"])  # the verdict that greys it out
    assert 20 not in valid_levels(world["profit"])


@given('the client submits level 20 based on its rendered profit of 25%')
def _(world):
    world["requested_level"] = 20


@given("the backend's own mark computes profit at 22%")
def _(world):
    world["backend_profit"] = D("22")


@then('the request is rejected (not clamped) and the UI refreshes  # EC-TPF-04')
def _(world):
    # Rejected: the backend's own mark fails the level...
    assert not is_armable(world["requested_level"], world["backend_profit"])
    # ...and NOT clamped: a lower level WOULD be armable, but the domain offers
    # no substitution — is_armable returns a verdict, never an alternative.
    assert is_armable(15, world["backend_profit"])
