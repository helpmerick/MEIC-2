"""ENT-10(4): a mid-day restart passes a FILTERED schedule; rows carry their
ORIGINAL numbers so entry_ids never collide with already-filled entries
(ORD-04 idempotency, RSK-04 exposure book)."""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal as D

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.persistent_state import PersistentState
from meic.composition.live_runtime import LiveRuntime, ScheduledRow
from meic.domain.events import EntrySkipped
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import ET

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
OPEN = datetime(2026, 7, 7, 9, 32, tzinfo=ET)
IS_CONDOR = lambda o: o.kind == "iron_condor"


class FastClock:
    """wait_until jumps to the deadline instead of blocking (see test_live_runtime.py)."""

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

    def __init__(self, clock, broker) -> None:
        self.clock = clock
        self.broker = broker
        self.events: list = []
        self.state = PersistentState(InMemoryStateStore())
        self.state.armed = True
        self.state.confirm_live = True
        self.state.stop_trading = False
        self.execute = ExecuteEntryAttempt(broker, clock, self.events, SPX)
        self.protected: list[str] = []

    async def _on_filled(self, entry_id, condor, stop=None):
        self.protected.append(entry_id)


def _runtime(comp: _Comp) -> LiveRuntime:
    async def selector(when, n, config=None):
        return _condor(n), None

    async def gates_provider():
        return GATES_PASS

    return LiveRuntime(comp, selector=selector, market_gates=gates_provider)


def test_run_day_honours_stamped_row_numbers_not_loop_index():
    """Rows carrying number=2 and number=3 (e.g. the remaining schedule after a
    mid-day restart that already filled #1) must record entry_ids #2 and #3 --
    never #1 and #2, which would collide with the entry already filled."""
    broker = FakeBroker(); broker.autofill(IS_CONDOR)
    comp = _Comp(FastClock(OPEN), broker)
    rows = [ScheduledRow(OPEN, number=2),
            ScheduledRow(OPEN + timedelta(minutes=30), number=3)]

    filled = asyncio.run(_runtime(comp).run_day("2026-07-07", rows))

    assert filled == 2
    assert comp.protected == ["2026-07-07#2", "2026-07-07#3"]  # STP-01 hand-off, ORIGINAL numbers
    assert [e for e in comp.events if isinstance(e, EntrySkipped)] == []


def test_run_day_falls_back_to_loop_index_when_number_is_unset():
    """Bare rows (no stamped number, e.g. the offline scheduler's plain datetimes)
    keep the pre-ENT-10 behaviour: numbered by position in the list."""
    broker = FakeBroker(); broker.autofill(IS_CONDOR)
    comp = _Comp(FastClock(OPEN), broker)
    rows = [ScheduledRow(OPEN), ScheduledRow(OPEN + timedelta(minutes=30))]

    filled = asyncio.run(_runtime(comp).run_day("2026-07-07", rows))

    assert filled == 2
    assert comp.protected == ["2026-07-07#1", "2026-07-07#2"]
