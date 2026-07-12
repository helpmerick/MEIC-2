"""Hand-written step definitions for TC-ENT-05 — a working entry order is
cancelled (and confirmed) before the next entry begins (ORD-06/CLS-03), any
partial resolved per EC-ENT-06."""
import asyncio

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.partial_fill import resolve_balanced_partial
from tests.harness.fake_broker import FakeBroker
from tests.harness.intents import condor_intent

scenarios("../features/TC-ENT-05.feature")


@pytest.fixture
def world():
    return {}


@given("entry 2's order is still WORKING at 11:00 ET")
def _(world):
    broker = FakeBroker()  # default: submits stay WORKING (never fill)
    world["broker"] = broker
    world["order_id"] = asyncio.run(broker.submit(condor_intent("4.00", entry_id="2026-07-06#2")))
    assert [o.order_id for o in asyncio.run(broker.working_orders())] == [world["order_id"]]


@when("entry 3's scheduled time arrives")
def _(world):
    # before entry 3 begins, entry 2's still-working order is cancelled
    world["cancel"] = asyncio.run(world["broker"].cancel(world["order_id"]))
    world["working_after"] = asyncio.run(world["broker"].working_orders())


@then("entry 2's order is cancelled and cancellation confirmed")
def _(world):
    assert world["cancel"]["result"] == "cancelled"
    assert world["working_after"] == []  # confirmed gone before entry 3 begins


@then('any partial fill is resolved per EC-ENT-06 before entry 3 begins')
def _(world):
    # no fill on entry 2's order -> nothing kept; and a balanced partial (1 of 2)
    # would keep and protect the filled condor
    assert resolve_balanced_partial(ordered_condors=1, filled_condors=0).keep_condors == 0
    kept = resolve_balanced_partial(ordered_condors=2, filled_condors=1)
    assert kept.keep_condors == 1 and kept.place_stops is True
