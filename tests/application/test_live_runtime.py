"""LiveRuntime — the wall-clock entry cadence. These tests exist to prove the
scheduler NEVER fires an entry while any block is in force."""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.persistent_state import PersistentState
from meic.composition.live_runtime import LiveRuntime
from meic.domain.events import DayArmed, DayCompleted, EntrySkipped, ReconciliationMismatch
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import ET

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
OPEN = datetime(2026, 7, 7, 9, 32, tzinfo=ET)
IS_CONDOR = lambda o: o.kind == "iron_condor"


class FastClock:
    """Wall-clock semantics with time fast-forwarded: wait_until jumps to the
    deadline instead of blocking (FakeClock blocks until externally advanced,
    which would deadlock a runtime that schedules into the future)."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    async def wait_until(self, when: datetime) -> None:
        if when > self._now:
            self._now = when

GATES_PASS = GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                          flatten_in_progress=False, market_open=True, market_halted=False,
                          data_fresh=True, session_valid=True, buying_power_ok=True)


def _condor(n=1) -> Condor:
    return Condor(entry_number=n, put_short=D("5990"), call_short=D("6060"),
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00"))


class _Comp:
    """Minimal stand-in for LiveComposition: the runtime only touches these."""

    def __init__(self, clock, broker, *, armed=True, confirm_live=True, stop_trading=False):
        self.clock = clock
        self.broker = broker
        self.events: list = []
        self.state = PersistentState(InMemoryStateStore())
        self.state.armed = armed
        self.state.confirm_live = confirm_live
        self.state.stop_trading = stop_trading
        self.execute = ExecuteEntryAttempt(broker, clock, self.events, SPX)
        self.protected: list[str] = []

    async def _on_filled(self, entry_id, condor, stop=None, fill_credit=None):
        self.protected.append(entry_id)


def _runtime(comp, *, selector=None, gates=GATES_PASS, **kw):
    async def default_selector(when, n, config=None):
        return _condor(n), None

    async def gates_provider():
        return gates

    return LiveRuntime(comp, selector=selector or default_selector,
                       market_gates=gates_provider, **kw)


def _times(count=2, step_min=30):
    return [OPEN + timedelta(minutes=step_min * i) for i in range(count)]


def _skips(events):
    return [(e.entry_number, e.reason) for e in events if isinstance(e, EntrySkipped)]


# --- the happy path: fires, fills, protects -----------------------------------

def test_fires_entries_at_their_times_and_protects_on_fill():
    broker = FakeBroker(); broker.autofill(IS_CONDOR)
    comp = _Comp(FastClock(OPEN), broker)
    filled = asyncio.run(_runtime(comp).run_day("2026-07-07", _times(2)))

    assert filled == 2
    assert comp.protected == ["2026-07-07#1", "2026-07-07#2"]  # STP-01 hand-off
    assert isinstance(comp.events[0], DayArmed)
    assert isinstance(comp.events[-1], DayCompleted)


# --- every block must prevent the entry ---------------------------------------

@pytest.mark.parametrize("kw,expected", [
    (dict(armed=False), "DISARMED"),
    (dict(confirm_live=False), "CONFIRM_LIVE_OFF"),
    (dict(stop_trading=True), "STOP_TRADING"),
])
def test_durable_gates_block_every_entry(kw, expected):
    broker = FakeBroker(); broker.autofill(IS_CONDOR)
    comp = _Comp(FastClock(OPEN), broker, **kw)
    filled = asyncio.run(_runtime(comp).run_day("2026-07-07", _times(2)))

    assert filled == 0 and comp.protected == []
    assert _skips(comp.events) == [(1, expected), (2, expected)]


def test_unresolved_reconcile_mismatch_blocks_entries():
    """A FOREIGN position (REC-02 mismatch on the log) blocks NEW entries."""
    broker = FakeBroker(); broker.autofill(IS_CONDOR)
    comp = _Comp(FastClock(OPEN), broker)
    comp.events.append(ReconciliationMismatch(detail="FOREIGN position SPXW_5990P"))

    filled = asyncio.run(_runtime(comp).run_day("2026-07-07", _times(2)))

    assert filled == 0
    assert _skips(comp.events) == [(1, "reconcile_pending"), (2, "reconcile_pending")]


def test_clock_drift_blocks_entries():
    broker = FakeBroker(); broker.autofill(IS_CONDOR)
    comp = _Comp(FastClock(OPEN), broker)
    rt = _runtime(comp, max_clock_drift_ms=250, measure_drift_ms=lambda: 900.0)

    filled = asyncio.run(rt.run_day("2026-07-07", _times(1)))

    assert filled == 0 and _skips(comp.events) == [(1, "clock_drift")]


def test_max_entries_per_day_caps_fills():
    broker = FakeBroker(); broker.autofill(IS_CONDOR)
    comp = _Comp(FastClock(OPEN), broker)
    filled = asyncio.run(_runtime(comp, max_entries_per_day=1).run_day("2026-07-07", _times(3)))

    assert filled == 1
    assert _skips(comp.events) == [(2, "max_entries"), (3, "max_entries")]


# --- selection: no condor => no order, with the selector's reason -------------

def test_selector_returning_none_skips_with_its_reason_and_submits_nothing():
    broker = FakeBroker(); broker.autofill(IS_CONDOR)
    comp = _Comp(FastClock(OPEN), broker)

    async def no_selection(when, n, config=None):
        return None, "incomplete_chain"

    filled = asyncio.run(_runtime(comp, selector=no_selection).run_day("2026-07-07", _times(2)))

    assert filled == 0 and comp.protected == []
    assert _skips(comp.events) == [(1, "incomplete_chain"), (2, "incomplete_chain")]


# --- ENT-08 warm-up runs ahead of the entry and never delays it ---------------

def test_warmup_runs_before_each_entry_and_does_not_delay_it():
    broker = FakeBroker(); broker.autofill(IS_CONDOR)
    clock = FastClock(OPEN - timedelta(minutes=5))  # boot before the entry time
    comp = _Comp(clock, broker)
    warmed: list[tuple] = []

    # ENT-08 (operator ruling 2026-07-11): the callable now also carries the
    # entry number + its SelectionConfig (row.selection), so the real warm-up
    # wiring can lock the STK-10 v1.55 baseline under the SAME key the fire
    # will use — see tests/application/test_live_app.py's warm-up capstone.
    async def warmup(when, n, config):
        warmed.append((clock.now(), n, config))  # observed at T-lead, before the entry time

    times = [OPEN]
    filled = asyncio.run(_runtime(comp, warmup=warmup, warmup_lead_seconds=60).run_day("2026-07-07", times))

    assert filled == 1
    assert warmed == [(OPEN - timedelta(seconds=60), 1, None)]  # ran at T-60, ahead of the entry
    assert clock.now() == OPEN                        # ENT-08: the clock never slipped
    assert comp.protected == ["2026-07-07#1"]         # entry still fired on time


# --- construction safety: no optimistic defaults ------------------------------

def test_runtime_cannot_be_built_without_selector_and_gates():
    comp = _Comp(FastClock(OPEN), FakeBroker())
    with pytest.raises(TypeError):
        LiveRuntime(comp)  # selector + market_gates are required, no defaults


# --- ENT-10(3)/STP-01: a cancel must never abandon a fill before its stop rests -

def test_cancelling_run_day_during_the_fill_handoff_still_places_the_stop():
    """A disarm (or /day/stop) cancels the day task. If the cancel lands AFTER an
    entry fills but WHILE the STP-01 hand-off is awaiting, an unshielded await
    unwinds and leaves a live condor with NO stop — the 2026-07-09 naked-position
    incident class. run_day itself must die (that is the point of the cancel);
    the in-flight protect hand-off must run to completion anyway."""
    async def scenario():
        broker = FakeBroker(); broker.autofill(IS_CONDOR)
        comp = _Comp(FastClock(OPEN), broker)
        handoff_started = asyncio.Event()
        release = asyncio.Event()
        protected: list[str] = []

        async def slow_on_filled(entry_id, condor, stop=None, fill_credit=None):
            handoff_started.set()
            await release.wait()           # the cancel lands in THIS window
            protected.append(entry_id)

        comp._on_filled = slow_on_filled
        task = asyncio.create_task(_runtime(comp).run_day("2026-07-07", _times(1)))
        await handoff_started.wait()       # fill committed at the broker; stop pending
        task.cancel()                      # ENT-10(3): the disarm/stop path
        await asyncio.sleep(0)             # deliver the cancellation
        release.set()                      # the stop-placement path proceeds

        with pytest.raises(asyncio.CancelledError):
            await task                     # the day task dies — desired

        for _ in range(10):                # let the shielded inner task finish
            await asyncio.sleep(0)
        assert protected == ["2026-07-07#1"], "cancellation left the fill UNPROTECTED"

    asyncio.run(scenario())
