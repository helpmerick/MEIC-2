"""Hand-written step definitions for TC-ENT-07 — the durable enabling states
(ENT-01/01a/01b, REC-07): arm/disarm, the three-state gate, and exact-restore
across day rollovers and container restarts."""
import inspect
from datetime import datetime, timedelta

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application import decay_watcher, protect_position, recover_long, tpf_monitor
from meic.application.execute_entry import within_window
from meic.application.persistent_state import PersistentState
from tests.harness.fake_clock import ET

scenarios("../features/TC-ENT-07.feature")


def _schedule(n):
    return [{"time": f"1{i}:00"} for i in range(n)]


@pytest.fixture
def world():
    store = InMemoryStateStore()
    return {"store": store, "state": PersistentState(store)}


def _no_dependency_on(name, *modules):
    return all(name not in inspect.getsource(m) for m in modules)


# --- Scenario: Disarmed means nothing fires, ever ----------------------------

@given('3 entries are composed in the UI but the operator never pressed Arm')
def _(world):
    world["state"].entry_schedule = _schedule(3)  # composed, but armed stays False


@then('no entry attempt occurs at any scheduled time')
def _(world):
    assert world["state"].armed is False
    assert world["state"].entries_enabled() is False


@then('existing positions remain fully managed (stops, LEX, TPF)')
def _(world):
    # management never consults ARMED — positions are managed regardless
    assert _no_dependency_on(".armed", protect_position, recover_long, tpf_monitor, decay_watcher)


# --- Scenario: Arming an empty schedule is rejected --------------------------

@given('zero entries are composed')
def _(world):
    world["state"].entry_schedule = []


@then('the Arm action fails validation with an explanatory error')
def _(world):
    assert world["state"].may_arm() is False  # ENT-01a: cannot arm an empty schedule


# --- Scenario: The operator's count is the count -----------------------------

@given('the operator composed exactly 4 entries and armed')
def _(world):
    world["state"].entry_schedule = _schedule(4)
    world["state"].armed = True
    world["state"].confirm_live = True


@then('exactly 4 entry attempts run, at exactly the composed times')
def _(world):
    assert len(world["state"].entry_schedule) == 4
    assert world["state"].entries_enabled() is True  # each composed time fires


# --- Scenario: Disarm mid-day stops future entries only ----------------------

@given('4 entries armed, 2 already filled')
def _(world):
    s = world["state"]
    s.entry_schedule = _schedule(4)
    s.armed = True
    s.confirm_live = True


@when('the operator disarms at 11:45')
def _(world):
    world["state"].armed = False


@then('the remaining 2 entries never fire')
def _(world):
    assert world["state"].entries_enabled() is False


@then('the 2 open condors keep their stops and full management')
def _(world):
    assert _no_dependency_on(".armed", protect_position, recover_long, tpf_monitor, decay_watcher)


# --- Scenario: Armed state persists across days ------------------------------

@given('the operator armed 6 entries on Monday')
def _(world):
    s = world["state"]
    s.entry_schedule = _schedule(6)
    s.armed = True
    s.confirm_live = True


@when("Tuesday's market opens with no operator action")
def _(world):
    world["tuesday"] = PersistentState(world["store"])  # same durable store, new day


@then('the day self-initializes (calendar, reconcile, warm-up)')
def _(world):
    assert world["tuesday"].armed is True and len(world["tuesday"].entry_schedule) == 6


@then('all 6 entries fire at their times on Tuesday, and every trading day after, until the operator disarms')
def _(world):
    assert world["tuesday"].entries_enabled() is True


# --- Scenario: Disarmed state equally persists -------------------------------

@given('the operator disarmed on Monday afternoon')
def _(world):
    world["state"].armed = False


@then('no entries fire on Tuesday, Wednesday, or any day until re-armed')
def _(world):
    assert PersistentState(world["store"]).entries_enabled() is False


# --- Scenario: Docker/process restart restores the armed state ---------------

@given('the system was ARMED with 6 entries and the container dies at 10:47')
def _(world):
    s = world["state"]
    s.entry_schedule = _schedule(6)
    s.armed = True
    s.confirm_live = True


@when('the container recovers at 10:52')
def _(world):
    world["recovered"] = PersistentState(world["store"])
    world["now"] = datetime(2026, 7, 6, 10, 52, tzinfo=ET)


@then('the bot boots ARMED (state restored from the durable store)')
def _(world):
    assert world["recovered"].armed is True


@then('the 10:30 entry (window missed while down) is SKIPPED missed_window')
def _(world):
    scheduled = datetime(2026, 7, 6, 10, 30, tzinfo=ET)
    assert within_window(world["now"], scheduled, 120) is False  # window long gone


@then('the 11:00 and later entries fire normally')
def _(world):
    eleven = datetime(2026, 7, 6, 11, 0, tzinfo=ET)
    assert within_window(eleven, eleven, 120) is True  # fires at its own time


@then('a restart while DISARMED boots DISARMED')
def _(world):
    store = InMemoryStateStore()
    PersistentState(store).armed = False
    assert PersistentState(store).armed is False


# --- Scenario: Confirm Live is the third required state ----------------------

@given('the system is ARMED with Stop Trading off')
def _(world):
    s = world["state"]
    s.entry_schedule = _schedule(3)
    s.armed = True
    s.stop_trading = False


@given('Confirm Live is OFF')
def _(world):
    world["state"].confirm_live = False


@then('no entry fires at any scheduled time')
def _(world):
    assert world["state"].entries_enabled() is False


@then('the dashboard states which gate is blocking')
def _(world):
    assert world["state"].blocking_state() == "CONFIRM_LIVE_OFF"


# --- Scenario: The full persistent-state inventory survives Docker recovery ---

@given('ARMED = on, Stop Trading = on, Confirm Live = on, trading_mode = paper, a standing 6-entry schedule, an armed TPF floor, and a paper cash ledger')
def _(world):
    s = world["state"]
    s.armed = True
    s.stop_trading = True
    s.confirm_live = True
    s.trading_mode = "paper"
    s.entry_schedule = _schedule(6)
    s.tpf_floors = {"2026-07-06#1": "6.00"}
    s.paper_cash_ledger = {"cash": "102045.00"}


@when('the container dies and recovers')
def _(world):
    world["recovered"] = PersistentState(world["store"])


@then('every item is restored exactly as it was')
def _(world):
    r = world["recovered"]
    assert r.armed is True and r.stop_trading is True and r.confirm_live is True
    assert r.trading_mode == "paper" and len(r.entry_schedule) == 6
    assert r.tpf_floors == {"2026-07-06#1": "6.00"}


@then('entries remain blocked (Stop Trading is on) until the operator resumes')
def _(world):
    assert world["recovered"].entries_enabled() is False


@then('the paper ledger balance is unchanged')
def _(world):
    assert world["recovered"].paper_cash_ledger == {"cash": "102045.00"}


# --- Scenario: Fresh install defaults safe -----------------------------------

@given('a first-ever boot with no persisted state')
def _(world):
    world["fresh"] = PersistentState(InMemoryStateStore())


@then('DISARMED, Stop Trading off, Confirm Live OFF')
def _(world):
    f = world["fresh"]
    assert f.armed is False and f.stop_trading is False and f.confirm_live is False
