"""STP-01 x ENT-09: cancelling a manual fire mid-hand-off must not abandon the
fill before its stop rests.

A panel ▶ runs inside a request handler; a client disconnect cancels that
handler. If the cancel lands AFTER the condor fills but WHILE _on_filled is
awaiting, an unshielded await unwinds and leaves a naked live condor — the same
window ENT-10(3) shields in run_day."""
import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.manual_entry import ManualEntry
from meic.application.persistent_state import PersistentState
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
NOW = datetime(2026, 7, 6, 10, 7, tzinfo=timezone.utc)
IS_CONDOR = lambda o: o.kind == "iron_condor"


class _Clock:
    def now(self):
        return NOW

    async def wait_until(self, when):
        return None


class _Comp:
    def __init__(self):
        self.broker = FakeBroker()
        self.broker.autofill(IS_CONDOR)
        self.events: list = []
        self.clock = _Clock()
        self.execute = ExecuteEntryAttempt(self.broker, self.clock, self.events, SPX)
        self.state = PersistentState(InMemoryStateStore())
        self.state.armed = True
        self.state.confirm_live = True
        self.state.stop_trading = False


async def _gates():
    return GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                        flatten_in_progress=False, market_open=True, market_halted=False,
                        data_fresh=True, session_valid=True, buying_power_ok=True)


def test_cancelling_fire_during_the_handoff_still_places_the_stop():
    async def scenario():
        comp = _Comp()
        handoff_started = asyncio.Event()
        release = asyncio.Event()
        protected: list[str] = []

        async def slow_on_filled(entry_id, condor, stop=None):
            handoff_started.set()
            await release.wait()           # the cancel lands in THIS window
            protected.append(entry_id)

        comp._on_filled = slow_on_filled

        async def selector(when, n, config=None):
            return Condor(entry_number=n, put_short=D("5990"), call_short=D("6060"),
                          put_long=D("5940"), call_long=D("6110"),
                          put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                          mid_credit=D("4.00"), min_total_credit=D("2.00"),
                          expiration=date(2026, 7, 6), contracts=1), None

        manual = ManualEntry(comp, selector, _gates, day=lambda: "2026-07-06")
        task = asyncio.create_task(
            manual.fire(press_id="p1", entry_number=1, row=None, confirmed=True))
        await handoff_started.wait()       # fill committed; stop hand-off pending
        task.cancel()                      # the client disconnected
        await asyncio.sleep(0)
        release.set()

        with pytest.raises(asyncio.CancelledError):
            await task                     # the request handler dies — fine

        for _ in range(10):                # let the shielded inner task finish
            await asyncio.sleep(0)
        assert protected == ["2026-07-06#1"], "cancellation left the fill UNPROTECTED"

    asyncio.run(scenario())
