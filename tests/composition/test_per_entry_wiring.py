"""ENT-04 / doc 06 §37: a schedule ROW's settings reach the order and the stop.

The v1.44 ruling made contracts (and target premium, wing width, credit floors,
stop basis/pct/markup) PER ENTRY. A per-entry override that selection or
protection quietly ignores is worse than no override at all: the operator would
see "2 contracts, 30-wide" on the row and the bot would trade 1 contract, 50-wide.

These tests follow one row's values all the way down:
    ResolvedEntry -> SelectionConfig -> Condor -> OrderIntent legs -> stop qty
"""
import asyncio
from datetime import date, datetime, time, timezone
from decimal import Decimal as D

import pytest

from meic.application.execute_entry import Condor, ExecuteEntryAttempt, StopParams
from meic.composition.live_runtime import LiveRuntime, ScheduledRow
from meic.composition.live_selection import SelectionConfig
from meic.domain.risk import OrderCap
from meic.domain.schedule import ResolvedEntry
from meic.domain.stop_policy import StopBasis
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
WHEN = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
IS_CONDOR = lambda o: o.kind == "iron_condor"


def _row(**over) -> ResolvedEntry:
    base = dict(time=time(10, 0), contracts=1, target_premium=D("3.00"), wing_width=D("50"),
                stop_loss_pct=95, stop_basis="total_credit", stop_rebate_markup=D("0.00"),
                min_short_premium=D("1.00"), min_total_credit=D("2.00"), probe_down_max=25,
                strike_method="premium", short_delta_target=D("0.10"))
    return ResolvedEntry(**{**base, **over})


# --- ResolvedEntry -> SelectionConfig ------------------------------------------

def test_selection_config_carries_every_per_entry_selection_override():
    row = _row(contracts=3, target_premium=D("2.00"), wing_width=D("30"),
               min_short_premium=D("0.75"), min_total_credit=D("1.50"))
    c = SelectionConfig.for_entry(row)
    assert (c.contracts, c.target_premium, c.wing_width) == (3, D("2.00"), D("30"))
    assert (c.min_short_premium, c.min_total_credit) == (D("0.75"), D("1.50"))


def test_completeness_pct_is_not_a_per_entry_override():
    """It describes the CHAIN, not the row (doc 06 §37 does not list it)."""
    assert SelectionConfig.for_entry(_row(), completeness_pct=D("80")).completeness_pct == D("80")


# --- ResolvedEntry -> StopParams ------------------------------------------------

def test_scheduled_row_exposes_this_rows_stop_settings():
    row = _row(stop_basis="short_premium", stop_loss_pct=150, stop_rebate_markup=D("0.50"))
    stop = ScheduledRow(WHEN, row).stop
    assert stop == StopParams(StopBasis.SHORT_PREMIUM, D("150"), D("0.50"))


def test_a_row_without_an_entry_uses_the_globals():
    bare = ScheduledRow(WHEN)
    assert bare.stop is None and bare.selection is None   # None => "use the service defaults"


# --- the row's contracts reach every leg of the order --------------------------

def _condor(contracts: int, width: D = D("50"), n: int = 1) -> Condor:
    return Condor(entry_number=n, put_short=D("5990"), call_short=D("6060"),
                  put_long=D("5990") - width, call_long=D("6060") + width,
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00"),
                  expiration=date(2026, 7, 6), contracts=contracts)


class _Capture(FakeBroker):
    def __init__(self):
        super().__init__()
        self.autofill(IS_CONDOR)
        self.intents = []

    async def submit(self, order):
        self.intents.append(order)
        return await super().submit(order)


def test_the_rows_contracts_size_every_leg_of_the_entry_order():
    from meic.application.entry_gates import GateSnapshot
    from tests.harness.fake_clock import FakeClock

    broker, events = _Capture(), []
    ex = ExecuteEntryAttempt(broker, FakeClock(WHEN), events, SPX)
    gates = GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                         flatten_in_progress=False, market_open=True, market_halted=False,
                         data_fresh=True, session_valid=True, buying_power_ok=True)
    outcome = asyncio.run(ex.attempt(day="2026-07-06", scheduled=WHEN,
                                     condor=_condor(3), gates=gates))
    assert outcome.status == "FILLED"
    intent = broker.intents[0]
    assert intent.contracts == 3 and all(leg.qty == 3 for leg in intent.legs)


def test_the_rows_wing_width_reaches_the_strikes_and_the_worst_case():
    """A 30-wide row must not be priced (or margined) as a 50-wide one."""
    narrow, wide = _condor(1, D("30")), _condor(1, D("50"))
    assert narrow.put_wing == D("5960") and wide.put_wing == D("5940")
    assert ExecuteEntryAttempt.worst_case(narrow) == D("2600")   # (30-4) × 100
    assert ExecuteEntryAttempt.worst_case(wide) == D("4600")     # (50-4) × 100


# --- LiveRuntime: the row drives selection, the stop, and the risk rails --------

class _Comp:
    def __init__(self, broker, events):
        from tests.harness.fake_clock import FakeClock
        self.broker, self.events = broker, events
        self.clock = _FastClock(WHEN)
        self.execute = ExecuteEntryAttempt(broker, self.clock, events, SPX)
        self.state = _State()
        self.protected: list = []

    async def _on_filled(self, entry_id, condor, stop=None, fill_credit=None):
        self.protected.append((entry_id, condor.contracts, stop))


class _State:
    armed = confirm_live = True
    stop_trading = False

    def entries_enabled(self):
        return True

    def blocking_state(self):
        return None


class _FastClock:
    """wait_until jumps forward — a live day's entry times are in the future."""

    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now

    async def wait_until(self, when):
        if when > self._now:
            self._now = when


def _gates():
    from meic.application.entry_gates import GateSnapshot

    async def provider():
        return GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                            flatten_in_progress=False, market_open=True, market_halted=False,
                            data_fresh=True, session_valid=True, buying_power_ok=True)
    return provider


def _runtime(broker, events, rows, **kw):
    comp = _Comp(broker, events)
    seen_configs = []

    async def selector(when, n, config=None):
        seen_configs.append(config)
        contracts = config.contracts if config else 1
        width = config.wing_width if config else D("50")
        return _condor(contracts, width, n), None

    rt = LiveRuntime(comp=comp, selector=selector, market_gates=_gates(), **kw)
    return rt, comp, seen_configs


def test_live_runtime_selects_and_protects_with_each_rows_own_settings():
    broker, events = _Capture(), []
    rows = [ScheduledRow(WHEN, _row(contracts=2)),
            ScheduledRow(WHEN.replace(hour=11), _row(contracts=1, stop_loss_pct=150,
                                                     stop_basis="short_premium"))]
    rt, comp, seen = _runtime(broker, events, rows)

    filled = asyncio.run(rt.run_day("2026-07-06", rows))

    assert filled == 2
    # the amended TC-ENT-03 scenario, through the LIVE runtime this time
    assert [i.contracts for i in broker.intents] == [2, 1]
    assert [c.contracts for c in seen] == [2, 1]                 # selection saw the row
    assert [p[1] for p in comp.protected] == [2, 1]              # stops sized to the fills
    assert comp.protected[1][2].basis is StopBasis.SHORT_PREMIUM  # row 2's own basis
    assert comp.protected[1][2].pct == D("150")


def test_live_runtime_accepts_bare_datetimes_and_uses_the_globals():
    broker, events = _Capture(), []
    rt, comp, seen = _runtime(broker, events, None)
    filled = asyncio.run(rt.run_day("2026-07-06", [WHEN]))
    assert filled == 1 and seen == [None] and comp.protected[0][2] is None


def test_live_runtime_enforces_rsk04_like_the_offline_scheduler():
    """The live path had NO risk rails at all before v1.44 item 5."""
    broker, events = _Capture(), []
    rows = [ScheduledRow(WHEN, _row(contracts=1)),
            ScheduledRow(WHEN.replace(hour=11), _row(contracts=1)),
            ScheduledRow(WHEN.replace(hour=12), _row(contracts=1))]
    rt, comp, _ = _runtime(broker, events, rows, max_day_risk=D("10000"))  # room for two 4600s

    filled = asyncio.run(rt.run_day("2026-07-06", rows))

    assert filled == 2
    skips = [e.reason for e in events if getattr(e, "reason", None)]
    assert "max_day_risk" in skips


def test_rsk04_counts_an_entry_even_when_its_number_is_not_its_loop_index():
    """Regression. ExecuteEntryAttempt keys its events off `condor.entry_number`;
    the runtime once keyed `_worst_case` off the loop index. When a selector numbers
    entries differently (a resumed day, a manual fire, a re-ordered schedule) the two
    disagreed and filled entries fell OUT of the RSK-04 total — silently, because the
    day still looked full. Both now read condor.entry_number."""
    broker, events = _Capture(), []
    comp = _Comp(broker, events)

    async def selector(when, n, config=None):
        return _condor(1, D("50"), n + 100), None      # numbers that are NOT 1,2,3

    rows = [ScheduledRow(WHEN), ScheduledRow(WHEN.replace(hour=11)),
            ScheduledRow(WHEN.replace(hour=12))]
    rt = LiveRuntime(comp=comp, selector=selector, market_gates=_gates(),
                     max_day_risk=D("10000"))          # room for exactly two 4600s

    filled = asyncio.run(rt.run_day("2026-07-06", rows))

    assert filled == 2                                  # not 3
    assert set(rt._worst_case) == {"2026-07-06#101", "2026-07-06#102"}


def test_live_runtime_enforces_the_order_cap_and_the_bp_gate():
    broker, events = _Capture(), []
    rows = [ScheduledRow(WHEN, _row())]
    rt, _, _ = _runtime(broker, events, rows, order_cap=OrderCap(cap=1, buffer=1))
    assert asyncio.run(rt.run_day("2026-07-06", rows)) == 0
    assert "order_cap" in [e.reason for e in events if getattr(e, "reason", None)]

    broker2, events2 = _Capture(), []
    rt2, _, _ = _runtime(broker2, events2, rows, buying_power=lambda: D("100"))
    assert asyncio.run(rt2.run_day("2026-07-06", rows)) == 0
    assert "insufficient_bp" in [e.reason for e in events2 if getattr(e, "reason", None)]


def test_a_2_contract_row_needs_twice_the_buying_power():
    """ENT-04 × ENT-03: the BP gate is priced at the row's own size."""
    rows_1 = [ScheduledRow(WHEN, _row(contracts=1))]
    rows_2 = [ScheduledRow(WHEN, _row(contracts=2))]

    b1, e1 = _Capture(), []
    rt1, _, _ = _runtime(b1, e1, rows_1, buying_power=lambda: D("5000"))
    assert asyncio.run(rt1.run_day("2026-07-06", rows_1)) == 1   # needs 4600

    b2, e2 = _Capture(), []
    rt2, _, _ = _runtime(b2, e2, rows_2, buying_power=lambda: D("5000"))
    assert asyncio.run(rt2.run_day("2026-07-06", rows_2)) == 0   # needs 9200
    assert "insufficient_bp" in [e.reason for e in e2 if getattr(e, "reason", None)]
