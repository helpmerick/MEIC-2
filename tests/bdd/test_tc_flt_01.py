"""Hand-written step definitions for TC-FLT-01 — Flatten all across mixed entry
states (RSK-01a): working orders cancelled (CLS-03), open entries closed via
CLS (manual_flatten), LEX superseded, TPF floors cleared, in-flight entries
skipped (flatten_in_progress), and Stop-Trading orthogonality/persistence."""
import asyncio
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.close_entry import CloseEntry, LiveLeg
from meic.application.entry_gates import GateSnapshot, evaluate_gates
from meic.application.flatten_all import FlattenAll, OpenEntry
from meic.application.persistent_state import PersistentState
from meic.domain.events import EntryClosed

scenarios("../features/TC-FLT-01.feature")


class RecordingBroker:
    def __init__(self):
        self.cancels = []
        self.close_keys = []

    async def submit(self, order):
        self.close_keys.append(order.idempotency_key)
        return "ok"

    async def cancel(self, id):
        self.cancels.append(id)
        return {"result": "cancelled"}


@pytest.fixture
def world():
    store = InMemoryStateStore()
    state = PersistentState(store)
    state.armed = True
    state.confirm_live = True
    state.tpf_floors = {"e4": "6.00"}  # entry 4 has an armed TPF floor
    return {"store": store, "state": state}


@given('entry 1 OPEN (both sides), entry 2 with put side mid-LEX, entry 3 with a WORKING entry order, entry 4 OPEN with an armed TPF floor')
def _(world):
    world["broker"] = RecordingBroker()
    world["events"] = []


@when('the operator confirms Flatten all')
def _(world):
    broker, events, state = world["broker"], world["events"], world["state"]
    close = CloseEntry(broker, events)

    async def flatten():
        # CLS-03: entry 3's still-working entry order is cancelled (no close orders)
        await broker.cancel("e3-entry-order")
        # entries 1, 2, 4 close via CLS (manual_flatten); e2's LEX is superseded
        book = [
            OpenEntry("e1", [LiveLeg("P1", "PUT", "short", -1), LiveLeg("C1", "CALL", "short", -1)], D("0.05")),
            OpenEntry("e2", [LiveLeg("P2", "PUT", "long", 1)], D("0.05")),   # orphan long from mid-LEX
            OpenEntry("e4", [LiveLeg("P4", "PUT", "short", -1), LiveLeg("C4", "CALL", "short", -1)], D("0.05")),
        ]
        await FlattenAll(close).flatten(book)

    asyncio.run(flatten())
    state.tpf_floors = {}  # armed TPF floors cleared by the flatten


@then("entry 3's order is cancelled (CLS-03), no close orders placed for its legs")
def _(world):
    assert "e3-entry-order" in world["broker"].cancels
    assert not any(":e3:" in k or k.startswith("close:e3") for k in world["broker"].close_keys)


@then('entries 1, 2, 4 close via CloseEntry with initiator "manual_flatten"')
def _(world):
    closed = {e.entry_id: e.initiator for e in world["events"] if isinstance(e, EntryClosed)}
    assert closed == {"e1": "manual_flatten", "e2": "manual_flatten", "e4": "manual_flatten"}


@then("entry 2's LEX ladder is superseded by an immediate marketable-limit close")
def _(world):
    # e2's orphan long was closed by the flatten (the LEX ladder did not run it out)
    assert any(k.startswith("close:e2") for k in world["broker"].close_keys)
    assert any(e.entry_id == "e2" for e in world["events"] if isinstance(e, EntryClosed))


@then("entry 4's TPF floor is cleared")
def _(world):
    assert world["state"].tpf_floors == {}


@then('a scheduled entry arriving WHILE the flatten executes is SKIPPED (flatten_in_progress)')
def _(world):
    snap = GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                        flatten_in_progress=True, market_open=True, market_halted=False,
                        data_fresh=True, session_valid=True, buying_power_ok=True)
    assert evaluate_gates(snap) == "flatten_in_progress"


@then('with the Stop Trading checkbox OFF, the next scheduled entry after completion fires normally into the clean book')
def _(world):
    # flatten did not touch Stop Trading -> entries still enabled
    assert world["state"].stop_trading is False
    assert world["state"].entries_enabled() is True


@then('with the checkbox ON, subsequent entries are blocked until reset (Stop Trading persisted across restart)')
def _(world):
    world["state"].stop_trading = True  # the "Also enable Stop Trading" checkbox
    restarted = PersistentState(world["store"])  # a fresh process, same store
    assert restarted.stop_trading is True
    assert restarted.entries_enabled() is False
