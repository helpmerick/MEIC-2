"""Hand-written step definitions for TC-CLS-01 — manual vs TPF close are
byte-identical (CLS-01/02)."""
import asyncio
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.close_entry import CloseEntry, LiveLeg
from meic.domain.events import EntryClosed
from tests.harness.fake_broker import FakeBroker

scenarios("../features/TC-CLS-01.feature")

LEGS = [LiveLeg("SPXW_5990P", "PUT", "short", -1), LiveLeg("SPXW_5940P", "PUT", "long", 1),
        LiveLeg("SPXW_6060C", "CALL", "short", -1), LiveLeg("SPXW_6110C", "CALL", "long", 1)]
STOPS = ["S1", "S2"]


class RecordingBroker:
    """Wraps FakeBroker, recording the exact (method, intent) request sequence."""

    def __init__(self):
        self._fake = FakeBroker()
        self.requests = []

    async def cancel(self, order_id):
        self.requests.append(("cancel", order_id))
        return await self._fake.cancel(order_id)

    async def submit(self, intent):
        # exclude the recorded initiator's effect: intents carry no initiator
        self.requests.append(("submit", dict(intent)))
        return await self._fake.submit(intent)


@pytest.fixture
def world():
    return {}


@given('two identical open entries A and B (same fills, same stops)')
def _(world):
    world["A"], world["B"] = RecordingBroker(), RecordingBroker()
    world["events_A"], world["events_B"] = [], []


@when('entry A is closed via the UI "Close trade" button')
def _(world):
    asyncio.run(CloseEntry(world["A"], world["events_A"]).close(
        "A", "manual", resting_stop_ids=STOPS, live_legs=LEGS, close_price=D("0.05")))


@when('entry B is closed via a TPF floor trigger')
def _(world):
    asyncio.run(CloseEntry(world["B"], world["events_B"]).close(
        "B", "take_profit", resting_stop_ids=STOPS, live_legs=LEGS, close_price=D("0.05")))


@then('the sequence of broker requests (cancels, close orders, prices, quantities) is identical')
def _(world):
    # normalize entry-id-specific idempotency keys so only structure/prices/qty compare
    def norm(reqs):
        out = []
        for method, payload in reqs:
            if method == "submit":
                p = {k: v for k, v in payload.items() if k != "idempotency_key"}
                out.append(("submit", p))
            else:
                out.append((method, payload))
        return out
    assert norm(world["A"].requests) == norm(world["B"].requests)


@then('only the recorded initiator differs: "manual" vs "take_profit"')
def _(world):
    a = [e for e in world["events_A"] if isinstance(e, EntryClosed)][0]
    b = [e for e in world["events_B"] if isinstance(e, EntryClosed)][0]
    assert a.initiator == "manual" and b.initiator == "take_profit"
