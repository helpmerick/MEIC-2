"""Hand-written step definitions for TC-RSK-07 — full crash-recovery SLA
(EC-RSK-05, REC-02..05)."""
import asyncio

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.reconcile import Reconcile, TrackedShort
from meic.domain.events import LongSaleStarted, StopReplaced
from tests.harness.fake_broker import FakeBroker

scenarios("../features/TC-RSK-07.feature")


@pytest.fixture
def world():
    return {}


@given('one open condor, one side mid-LEX, one working entry order')
def _(world):
    """Broker + event log survive the crash (they live outside the bot). Set
    up: condor A both shorts protected (stops resting); side B short stopped,
    long-sale mid-LEX; a stale working entry order C whose window elapsed."""
    broker = FakeBroker()
    a_put_stop = asyncio.run(broker.submit({"type": "stop_market", "leg": "short_put", "entry_id": "A"}))
    a_call_stop = asyncio.run(broker.submit({"type": "stop_market", "leg": "short_call", "entry_id": "A"}))
    stale_order = asyncio.run(broker.submit({"type": "limit", "kind": "iron_condor", "legs": 4, "entry_id": "C"}))
    working = {o.order_id for o in asyncio.run(broker.working_orders())}

    world["broker"] = broker
    world["tracked"] = [
        TrackedShort("A", "PUT", "SPXW_5990P", stop_order_id=a_put_stop, stop_filled=False),
        TrackedShort("A", "CALL", "SPXW_6060C", stop_order_id=a_call_stop, stop_filled=False),
        TrackedShort("B", "PUT", "SPXW_5985P", stop_order_id="B_put_stop", stop_filled=True),
    ]
    world["working"] = working
    world["stale"] = [stale_order]


@when('the process is killed and restarted')
def _(world):
    events = []
    rec = Reconcile(world["broker"], events)
    plan = rec.plan(
        tracked_shorts=world["tracked"],
        broker_working_order_ids=world["working"],
        mid_lex_sides=[("B", "PUT")],
        stale_entry_order_ids=world["stale"],
    )
    asyncio.run(rec.execute(plan))
    world["events"], world["plan"] = events, plan
    world["working_after"] = {o.order_id for o in asyncio.run(world["broker"].working_orders())}


@then('within recovery_sla_seconds every short is covered by a confirmed resting stop')
def _(world):
    assert world["plan"].place_stops == []       # condor A's stops re-attached
    assert world["plan"].user_unprotected == []  # nothing operator-cancelled


@then('the LEX ladder has resumed')
def _(world):
    resumed = {(e.entry_id, e.side) for e in world["events"] if isinstance(e, LongSaleStarted)}
    assert ("B", "PUT") in resumed


@then('the stale entry order is cancelled (window elapsed)')
def _(world):
    stale_id = world["stale"][0]
    assert world["broker"]._orders[stale_id].status == "CANCELLED"
    assert stale_id not in world["working_after"]


@then('zero duplicate orders exist at the broker')
def _(world):
    assert not any(isinstance(e, StopReplaced) for e in world["events"])
    stops = [o for o in world["broker"]._orders.values()
             if o.intent.get("type") == "stop_market" and o.status != "CANCELLED"]
    keys = [(o.intent.get("entry_id"), o.intent.get("leg")) for o in stops]
    assert len(keys) == len(set(keys))  # no duplicate (entry, leg) stop
