"""ENT-05 (v1.44) x the 2026-07-13 day-scoping fix.

The live bug: `ManualEntry._filled_today()` called
`day_report(self._comp.events).entries_filled` with NO day argument, and
`day_report` itself folded the ENTIRE event log with no day filter at all --
so a fill from a PRIOR day that never reached a terminal state (its
settlement was never captured, so it lingers in the fold forever) counted
toward every LATER day's `max_entries_per_day` cap. In production this meant
a manual ▶ fire could see "2 filled today" when only 1 had actually filled
today, and once the cap was reached once by a stale prior-day entry, manual
fires would be blocked permanently.

The fix: `_filled_today()` now passes `self.today()` explicitly to
`day_report`, so ENT-05 counts only TODAY's fills.
"""
import asyncio
from datetime import date, datetime, time, timezone
from decimal import Decimal as D

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.entry_gates import GateSnapshot
from meic.application.event_log import EventLog
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.manual_entry import ManualEntry
from meic.application.persistent_state import PersistentState
from meic.domain.events import CondorFilled
from meic.domain.schedule import ResolvedEntry
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
NOW = datetime(2026, 7, 13, 10, 7, tzinfo=timezone.utc)
IS_CONDOR = lambda o: o.kind == "iron_condor"


def _row(**over) -> ResolvedEntry:
    base = dict(time=time(10, 0), contracts=1, target_premium=D("3.00"), wing_width=D("50"),
                stop_loss_pct=95, stop_basis="total_credit", stop_rebate_markup=D("0.00"),
                min_short_premium=D("1.00"), min_total_credit=D("2.00"), probe_down_max=25,
                strike_method="premium", short_delta_target=D("0.10"))
    return ResolvedEntry(**{**base, **over})


def _condor(n=1, contracts=1):
    return Condor(entry_number=n, put_short=D("5990"), call_short=D("6060"),
                  put_long=D("5940"), call_long=D("6110"),
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00"),
                  expiration=date(2026, 7, 13), contracts=contracts)


class _Clock:
    def now(self):
        return NOW

    async def wait_until(self, when):
        return None


class _Comp:
    def __init__(self, events):
        self.broker = FakeBroker()
        self.broker.autofill(IS_CONDOR)
        self.events = events
        self.clock = _Clock()
        self.execute = ExecuteEntryAttempt(self.broker, self.clock, self.events, SPX)
        self.state = PersistentState(InMemoryStateStore())
        self.state.armed = True
        self.state.confirm_live = True
        self.state.stop_trading = False
        self.protected: list = []

    async def _on_filled(self, entry_id, condor, stop=None, fill_credit=None):
        self.protected.append(entry_id)


async def _gates():
    return GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                        flatten_in_progress=False, market_open=True, market_halted=False,
                        data_fresh=True, session_valid=True, buying_power_ok=True)


def _manual(comp, *, max_entries):
    async def selector(when, n, config=None, put_floor=None, call_floor=None):
        return _condor(n, config.contracts if config else 1), None

    return ManualEntry(comp, selector, _gates, max_entries_per_day=max_entries,
                       day=lambda: "2026-07-13")


def test_a_prior_days_filled_entry_does_not_consume_todays_ent05_slot():
    """THE bug: 2026-07-10#1 filled and never reached a terminal state (its
    settlement was never captured), so it lingers in the fold. That must NOT
    count toward 2026-07-13's max_entries_per_day=1 cap -- a fresh manual fire
    on 2026-07-13 must still be allowed to fill."""
    events = EventLog(config_version="v1.62")
    events.append(CondorFilled(entry_id="2026-07-10#1", net_credit=D("5.20")))  # yesterday, still open

    comp = _Comp(events)
    manual = _manual(comp, max_entries=1)

    result = asyncio.run(manual.fire(press_id="p1", entry_number=2, row=_row(), confirmed=True))

    assert result["result"] == "filled"
    assert result["entry_id"] == "2026-07-13#2"
    assert comp.protected == ["2026-07-13#2"]


def test_todays_own_fill_still_counts_toward_the_cap():
    """The gate itself must still work for TODAY's own fills -- only the
    cross-day leak was the bug; ENT-05 is not disabled by this fix."""
    events = EventLog(config_version="v1.62")
    events.append(CondorFilled(entry_id="2026-07-13#1", net_credit=D("4.00")))  # today, already filled

    comp = _Comp(events)
    manual = _manual(comp, max_entries=1)

    result = asyncio.run(manual.fire(press_id="p1", entry_number=2, row=_row(), confirmed=True))

    assert result == {"result": "skipped", "reason": "max_entries"}
