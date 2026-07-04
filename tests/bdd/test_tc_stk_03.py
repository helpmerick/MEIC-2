"""Hand-written step definitions for TC-STK-03 — STK-05 gross-premium floor (Phase 3)."""
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then

from meic.domain.gates import GatesFailed, GatesPassed, check_credit_gates

scenarios("../features/TC-STK-03.feature")


@pytest.fixture
def world():
    return {"orders": []}


@given("the short put's mid = 0.80 and min_short_premium = 1.00")
def _(world):
    world["result"] = check_credit_gates(
        put_short_mid=D("0.80"), call_short_mid=D("1.25"),
        total_net_credit_mid=D("2.30"),
        min_short_premium=D("1.00"), min_total_credit=D("2.00"),
    )
    if isinstance(world["result"], GatesPassed):  # only a passing gate builds an order
        world["orders"].append("condor")


@then('the entry is SKIPPED with reason "insufficient_credit"')
def _(world):
    assert world["result"] == GatesFailed("insufficient_credit")


@then('no order of any kind is submitted   # single-side entries prohibited')
def _(world):
    assert world["orders"] == []
