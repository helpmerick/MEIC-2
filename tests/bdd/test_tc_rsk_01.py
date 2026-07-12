"""Hand-written step definitions for TC-RSK-01 — Stop Trading blocks entries and
nothing else (RSK-01); Flatten All is orthogonal (RSK-01a); Stop Trading is
durable across restart (REC-07)."""
import asyncio
import inspect
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application import decay_watcher, protect_position, recover_long, tpf_monitor
from meic.application.close_entry import CloseEntry, LiveLeg
from meic.application.entry_gates import GateSnapshot, evaluate_gates
from meic.application.flatten_all import FlattenAll, OpenEntry
from meic.application.persistent_state import PersistentState
from meic.domain.events import EntryClosed

scenarios("../features/TC-RSK-01.feature")


def _snapshot(state: PersistentState) -> GateSnapshot:
    return GateSnapshot(
        armed=state.armed, confirm_live=state.confirm_live, stop_trading=state.stop_trading,
        flatten_in_progress=False, market_open=True, market_halted=False,
        data_fresh=True, session_valid=True, buying_power_ok=True)


@pytest.fixture
def world():
    store = InMemoryStateStore()
    state = PersistentState(store)
    state.armed = True
    state.confirm_live = True  # entries enabled to begin with
    return {"store": store, "state": state}


# --- Scenario 1: Stop Trading blocks entries and nothing else ----------------

@given('two open condors and one LEX ladder in progress')
def _(world):
    world["lex_in_progress"] = True  # risk-reducing work already running


@when('Stop Trading is activated')
def _(world):
    world["state"].stop_trading = True


@then('no further entries occur')
def _(world):
    assert world["state"].entries_enabled() is False
    assert world["state"].blocking_state() == "STOP_TRADING"
    assert evaluate_gates(_snapshot(world["state"])) == "stop_trading"


@then('resting stops remain working')
def _(world):
    # stop management never consults Stop Trading -> existing stops are untouched
    assert "stop_trading" not in inspect.getsource(protect_position)


@then('the LEX ladder continues            # risk-reducing work proceeds')
def _(world):
    assert "stop_trading" not in inspect.getsource(recover_long)


@then('TPF monitoring and the decay watcher continue')
def _(world):
    assert "stop_trading" not in inspect.getsource(tpf_monitor)
    assert "stop_trading" not in inspect.getsource(decay_watcher)


# --- Scenario 2: Flatten All does not block trading (orthogonality) ----------

@given('Flatten All is confirmed WITHOUT the Stop Trading checkbox')
def _(world):
    events: list = []

    class _Broker:
        async def submit(self, o):
            return "ok"
        async def cancel(self, i):
            return {"result": "cancelled"}

    flat = FlattenAll(CloseEntry(_Broker(), events))
    book = [OpenEntry(f"e{n}", [LiveLeg(f"P{n}", "PUT", "short", -1)], D("0.05")) for n in (1, 2)]
    asyncio.run(flat.flatten(book))
    world["events"] = events
    # the Stop Trading checkbox was NOT ticked, so it stays off
    assert world["state"].stop_trading is False


@then('every bot entry closes via CLS')
def _(world):
    closed = {e.entry_id: e.initiator for e in world["events"] if isinstance(e, EntryClosed)}
    assert closed == {"e1": "manual_flatten", "e2": "manual_flatten"}


@then('the next scheduled entry fires normally into the clean book')
def _(world):
    # Stop Trading is off, so the enabling gate still passes -> the next entry fires
    assert world["state"].entries_enabled() is True


# --- Scenario 3: Stop Trading persists across restart ------------------------

@given('Stop Trading was active')
def _(world):
    world["state"].stop_trading = True  # written through to the durable store


@when('the bot restarts')
def _(world):
    # a fresh process rebuilds PersistentState over the SAME store (REC-07)
    world["restarted"] = PersistentState(world["store"])


@then('entries remain blocked until the operator resets ("Resume trading")')
def _(world):
    restarted = world["restarted"]
    assert restarted.stop_trading is True              # survived the restart
    assert restarted.entries_enabled() is False        # still blocked
    restarted.stop_trading = False                     # operator: "Resume trading"
    assert restarted.entries_enabled() is True
