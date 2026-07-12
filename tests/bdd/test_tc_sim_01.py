"""Hand-written step definitions for TC-SIM-01 — paper-mode trade-through fill
(SIM-02)."""
import asyncio
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.adapters.sim.simulated_broker import SimulatedBroker
from tests.harness.intents import condor_intent

scenarios("../features/TC-SIM-01.feature")


@pytest.fixture
def world():
    return {}


@given('a condor limit at 2.30 net credit')
def _(world):
    world["broker"] = SimulatedBroker(tick=D("0.05"), fill_through_ticks=1)
    world["oid"] = asyncio.run(world["broker"].submit(condor_intent("2.30")))


@when('the real net mid touches 2.30 exactly')
def _(world):
    # mid == limit (touch), natural below limit -> must NOT fill
    world["touch_filled"] = world["broker"].try_fill_limit(
        world["oid"], natural=D("2.20"), mid=D("2.30"), is_credit=True)


@then('the order does NOT fill')
def _(world):
    assert world["touch_filled"] is False
    assert len(asyncio.run(world["broker"].working_orders())) == 1


@when('the natural price satisfies 2.30 OR the mid reaches 2.35 (one tick through)')
def _(world):
    # mid 2.35 = one 0.05 tick through the 2.30 credit limit -> fills
    world["through_filled"] = world["broker"].try_fill_limit(
        world["oid"], natural=D("2.25"), mid=D("2.35"), is_credit=True)


@then('the order fills all-or-nothing with per-leg prices allocated from current quotes')
def _(world):
    assert world["through_filled"] is True
    assert asyncio.run(world["broker"].working_orders()) == []  # all-or-nothing, now filled
    fills = asyncio.run(world["broker"].fills_since(None))
    assert len(fills) == 1
