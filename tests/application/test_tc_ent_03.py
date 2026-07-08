"""TC-ENT-03 (ENT-04/ENT-05), amended for spec v1.44.

Each schedule ROW carries its own `contracts` (1-10). The entry order — and every
leg of it — trades that row's size, not a global knob. RSK-04's day exposure is
the SUM of each entry's own worst case (`2 x wc1 + 1 x wc2`), never `n x max(wc)`.

`contracts_per_entry` survives in config only as the UI's row pre-fill, which is
why `ExecuteEntryAttempt` no longer accepts it at all.
"""
import asyncio
from datetime import date, datetime
from decimal import Decimal as D

import pytest

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.persistent_state import PersistentState
from meic.application.run_trading_day import RunTradingDay, ScheduledEntry
from meic.domain.projection import day_report
from meic.domain.risk import day_worst_case, worst_case_loss
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import ET, FakeClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
OPEN = datetime(2026, 7, 6, 9, 32, tzinfo=ET)
IS_CONDOR = lambda o: o.kind == "iron_condor"


def _condor(n: int, contracts: int = 1) -> Condor:
    """A condor with real wings — the ACL needs all four strikes."""
    put_short, call_short = D(str(5990 - n)), D(str(6060 + n))
    return Condor(entry_number=n, put_short=put_short, call_short=call_short,
                  put_long=put_short - 50, call_long=call_short + 50,
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00"),
                  expiration=date(2026, 7, 6), contracts=contracts)


def _gates() -> GateSnapshot:
    return GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                        flatten_in_progress=False, market_open=True, market_halted=False,
                        data_fresh=True, session_valid=True, buying_power_ok=True)


class CaptureBroker(FakeBroker):
    def __init__(self):
        super().__init__()
        self.autofill(IS_CONDOR)
        self.entry_intents = []

    async def submit(self, order):
        if IS_CONDOR(order):
            self.entry_intents.append(order)
        return await super().submit(order)


# --- ENT-04 (v1.44): the size comes from the ROW ------------------------------

@pytest.mark.parametrize("contracts", [1, 2, 3, 10])
def test_tc_ent_03_order_quantity_equals_the_rows_own_contracts(contracts):
    """ENT-04: the entry order carries THIS ROW's contracts, on every leg."""
    broker, events, clock = CaptureBroker(), [], FakeClock(OPEN)
    ex = ExecuteEntryAttempt(broker, clock, events, SPX)
    outcome = asyncio.run(ex.attempt(day="2026-07-06", scheduled=OPEN,
                                     condor=_condor(1, contracts), gates=_gates()))
    assert outcome.status == "FILLED"

    intent = broker.entry_intents[0]
    assert intent.contracts == contracts
    assert len(intent.legs) == 4
    assert all(leg.qty == contracts for leg in intent.legs)


def test_tc_ent_03_rows_of_2_and_1_produce_fills_of_2_and_1():
    """The amended scenario, verbatim: schedule rows 2 and 1 => fills of 2 and 1.
    A single global knob could not express this."""
    broker, events, clock = CaptureBroker(), [], FakeClock(OPEN)
    ex = ExecuteEntryAttempt(broker, clock, events, SPX)
    for n, contracts in ((1, 2), (2, 1)):
        asyncio.run(ex.attempt(day="2026-07-06", scheduled=OPEN,
                               condor=_condor(n, contracts), gates=_gates()))
    assert [i.contracts for i in broker.entry_intents] == [2, 1]


def test_tc_ent_03_execute_entry_has_no_global_contracts_knob():
    """v1.44 removed it: `contracts_per_entry` is row pre-fill only. If it came
    back as a service-level default, every row would silently trade the same size."""
    broker, events, clock = CaptureBroker(), [], FakeClock(OPEN)
    with pytest.raises(TypeError, match="contracts_per_entry"):
        ExecuteEntryAttempt(broker, clock, events, SPX, contracts_per_entry=3)


# --- RSK-04 (v1.44): the day's exposure is a SUM of per-entry worst cases -------

def test_tc_ent_03_rsk04_sums_per_entry_worst_cases_never_n_times_max():
    """2 contracts at wc1 + 1 contract at wc2 — NOT 3 x max(wc)."""
    wc1 = worst_case_loss(D("50"), D("4.00"), contracts=2)   # (50-4) x 100 x 2
    wc2 = worst_case_loss(D("30"), D("2.00"), contracts=1)   # (30-2) x 100 x 1
    assert (wc1, wc2) == (D("9200"), D("2800"))

    total = day_worst_case([(D("50"), D("4.00"), 2), (D("30"), D("2.00"), 1)])
    assert total == D("12000") == wc1 + wc2

    n_times_max = 3 * max(wc1, wc2)
    assert n_times_max == D("27600") and total != n_times_max


# --- ENT-05: the day never exceeds max_entries_per_day FILLS -------------------

def test_tc_ent_03_day_never_exceeds_max_entries_per_day_fills():
    """ENT-05: with 5 scheduled but a cap of 2, only 2 fill; the rest skip
    `max_entries` and are not retried."""
    broker, events = CaptureBroker(), []
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
