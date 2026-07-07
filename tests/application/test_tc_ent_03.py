"""TC-ENT-03 (ENT-04/ENT-05): the entry order trades contracts_per_entry per
leg; a day never exceeds max_entries_per_day FILLS (skips are not retried)."""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal as D

from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.persistent_state import PersistentState
from meic.application.run_trading_day import RunTradingDay, ScheduledEntry
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.domain.projection import day_report
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import ET, FakeClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
OPEN = datetime(2026, 7, 6, 9, 32, tzinfo=ET)
IS_CONDOR = lambda o: isinstance(o, dict) and o.get("kind") == "iron_condor"


def _condor(n: int) -> Condor:
    return Condor(entry_number=n, put_short=D(str(5990 - n)), call_short=D(str(6060 + n)),
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00"))


def _gates() -> GateSnapshot:
    return GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                        flatten_in_progress=False, market_open=True, market_halted=False,
                        data_fresh=True, session_valid=True, buying_power_ok=True)


def test_tc_ent_03_order_quantity_equals_contracts_per_entry():
    """ENT-04: the entry order carries contracts_per_entry contracts per leg."""
    class CaptureBroker(FakeBroker):
        def __init__(self):
            super().__init__()
            self.autofill(IS_CONDOR)
            self.entry_intents = []
        async def submit(self, order):
            if IS_CONDOR(order):
                self.entry_intents.append(order)
            return await super().submit(order)

    broker = CaptureBroker()
    events: list = []
    clock = FakeClock(OPEN)
    ex = ExecuteEntryAttempt(broker, clock, events, SPX, contracts_per_entry=3)
    outcome = asyncio.run(ex.attempt(day="2026-07-06", scheduled=OPEN,
                                     condor=_condor(1), gates=_gates()))
    assert outcome.status == "FILLED"
    assert broker.entry_intents[0]["contracts"] == 3  # ENT-04


def test_tc_ent_03_day_never_exceeds_max_entries_per_day_fills():
    """ENT-05: with 5 scheduled but a cap of 2, only 2 fill; the rest skip
    `max_entries` and are not retried."""
    broker, events = FakeBroker(), []
    broker.autofill(IS_CONDOR)
    state = PersistentState(InMemoryStateStore())
    state.entry_schedule = [{"time": "x"}] * 5
    state.armed = True
    state.confirm_live = True
    clock = FakeClock(OPEN)
    day = RunTradingDay(clock, state, ExecuteEntryAttempt(broker, clock, events, SPX),
                        events, max_entries_per_day=2)
    schedule = [ScheduledEntry(OPEN, _condor(i + 1)) for i in range(5)]

    filled = asyncio.run(day.run("2026-07-06", schedule))
    assert filled == 2                                   # never exceeds the cap
    reasons = {r for _, r in day_report(events).skips}
    assert "max_entries" in reasons                      # the surplus skipped, not retried
    assert day_report(events).entries_filled == 2
