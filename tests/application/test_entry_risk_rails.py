"""RSK-04 (max exposure) and RSK-08 (order cap) on the SHARED entry path.

Before v1.44/v1.45, `exceeds_max_day_risk` existed in the domain and NOTHING
called it: the bot had no max-day-risk protection at all. It is enforced here,
inside ExecuteEntryAttempt.attempt(), so a manual ENT-09 fire — which bypasses
only the ENT-02 window — crosses the identical rails (TC-ENT-08 scenario 4).
"""
import asyncio
from datetime import date, datetime
from decimal import Decimal as D

import pytest

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.entry_gates import GateSnapshot, RiskSnapshot, evaluate_risk
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.persistent_state import PersistentState
from meic.application.run_trading_day import RunTradingDay, ScheduledEntry
from meic.domain.projection import day_report
from meic.domain.risk import OrderCap
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import ET, FakeClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
OPEN = datetime(2026, 7, 6, 9, 32, tzinfo=ET)
IS_CONDOR = lambda o: o.kind == "iron_condor"


def _condor(n: int = 1, contracts: int = 1, width: D = D("50")) -> Condor:
    put_short, call_short = D(str(5990 - n)), D(str(6060 + n))
    return Condor(entry_number=n, put_short=put_short, call_short=call_short,
                  put_long=put_short - width, call_long=call_short + width,
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00"),
                  expiration=date(2026, 7, 6), contracts=contracts)


def _gates() -> GateSnapshot:
    return GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                        flatten_in_progress=False, market_open=True, market_halted=False,
                        data_fresh=True, session_valid=True, buying_power_ok=True)


def _attempt(condor, risk, *, bypass_window=False):
    broker, events, clock = FakeBroker(), [], FakeClock(OPEN)
    broker.autofill(IS_CONDOR)
    ex = ExecuteEntryAttempt(broker, clock, events, SPX)
    outcome = asyncio.run(ex.attempt(day="2026-07-06", scheduled=OPEN, condor=condor,
                                     gates=_gates(), risk=risk, bypass_window=bypass_window))
    return outcome, broker, events


# --- the worst case is priced from the condor, not from the caller -------------

def test_worst_case_uses_the_wider_wing_and_the_rows_contracts():
    """(width − credit) × 100 × contracts. Only one side settles ITM, but we do
    not choose which — so the WIDER wing governs."""
    assert ExecuteEntryAttempt.worst_case(_condor(1, 1)) == D("4600")   # (50-4)*100
    assert ExecuteEntryAttempt.worst_case(_condor(1, 2)) == D("9200")   # ... × 2 contracts
    assert ExecuteEntryAttempt.worst_case(_condor(1, 1, width=D("30"))) == D("2600")


def test_a_caller_cannot_under_report_its_own_entrys_risk():
    """RSK-04 must not be spoofable: attempt() re-prices `new_worst_case` from the
    condor, ignoring whatever the snapshot claimed."""
    lying = RiskSnapshot(new_worst_case=D("1"),          # "this entry risks a dollar"
                         open_worst_cases=(D("6000"),), max_day_risk=D("10000"))
    outcome, broker, _ = _attempt(_condor(1, 1), lying)  # real wc is 4600; 6000+4600 > 10000
    assert outcome.status == "SKIPPED" and outcome.reason == "max_day_risk"
    assert asyncio.run(broker.working_orders()) == []    # nothing reached the broker


# --- RSK-04 blocks the entry, on the shared path -------------------------------

def test_rsk04_blocks_when_summed_worst_case_exceeds_max_day_risk():
    risk = RiskSnapshot(new_worst_case=D("0"), open_worst_cases=(D("6000"),),
                        max_day_risk=D("10000"))       # 6000 + 4600 = 10600 > 10000
    outcome, _, events = _attempt(_condor(), risk)
    assert (outcome.status, outcome.reason) == ("SKIPPED", "max_day_risk")
    assert ("max_day_risk" in {r for _, r in day_report(events).skips})


def test_rsk04_allows_the_entry_that_exactly_fits():
    risk = RiskSnapshot(new_worst_case=D("0"), open_worst_cases=(D("5400"),),
                        max_day_risk=D("10000"))       # 5400 + 4600 == 10000, not >
    outcome, _, _ = _attempt(_condor(), risk)
    assert outcome.status == "FILLED"


def test_rsk04_sums_per_entry_worst_cases_never_n_times_max():
    """v1.44: two open entries at 9200 and 2800 expose 12000, not 3 × 9200."""
    risk = RiskSnapshot(new_worst_case=D("0"), open_worst_cases=(D("9200"), D("2800")),
                        max_day_risk=D("20000"))       # 12000 + 4600 = 16600 <= 20000
    assert _attempt(_condor(), risk)[0].status == "FILLED"
    # had we used 3 × max = 27600, this same entry would have been vetoed


def test_no_max_day_risk_configured_means_no_rsk04_veto():
    """None = paper/tests. Live mode cannot enable without it (doc 06 §169)."""
    risk = RiskSnapshot(new_worst_case=D("0"), open_worst_cases=(D("999999"),), max_day_risk=None)
    assert _attempt(_condor(), risk)[0].status == "FILLED"


# --- RSK-08 order cap, and its ordering relative to RSK-04 ---------------------

def test_rsk08_order_cap_blocks_a_new_entry():
    risk = RiskSnapshot(new_worst_case=D("0"), order_cap_allows_entry=False)
    outcome, broker, _ = _attempt(_condor(), risk)
    assert (outcome.status, outcome.reason) == ("SKIPPED", "order_cap")
    assert asyncio.run(broker.working_orders()) == []


def test_order_cap_is_checked_before_rsk04():
    """ENT-09 spells the chain: '... buying power ∧ order cap ∧ RSK-04'."""
    both_fail = RiskSnapshot(new_worst_case=D("99999"), open_worst_cases=(D("99999"),),
                             max_day_risk=D("1"), order_cap_allows_entry=False)
    assert evaluate_risk(both_fail) == "order_cap"


def test_evaluate_risk_passes_when_every_rail_is_clear():
    assert evaluate_risk(RiskSnapshot(new_worst_case=D("4600"),
                                      open_worst_cases=(D("1000"),),
                                      max_day_risk=D("10000"),
                                      buying_power=D("50000"))) is None


# --- ENT-03 buying power: compared to THIS condor's margin ---------------------

def test_bp_gate_compares_against_the_new_condors_own_margin():
    """ENT-03: 'buying power sufficient for worst-case margin of the new condor'.
    4599 cannot carry a 4600 condor; 4600 exactly can."""
    outcome, broker, _ = _attempt(_condor(), RiskSnapshot(new_worst_case=D("0"),
                                                          buying_power=D("4599")))
    assert (outcome.status, outcome.reason) == ("SKIPPED", "insufficient_bp")
    assert asyncio.run(broker.working_orders()) == []   # never submitted

    assert _attempt(_condor(), RiskSnapshot(new_worst_case=D("0"),
                                            buying_power=D("4600")))[0].status == "FILLED"


def test_bp_scales_with_per_entry_contracts():
    """A 2-contract condor needs twice the buying power (ENT-04 / SIM-04)."""
    tight = RiskSnapshot(new_worst_case=D("0"), buying_power=D("5000"))
    assert _attempt(_condor(1, 1), tight)[0].status == "FILLED"            # needs 4600
    assert _attempt(_condor(1, 2), tight)[0].reason == "insufficient_bp"   # needs 9200


def test_bp_is_checked_before_the_order_cap_and_rsk04():
    """ENT-09's chain: '... buying power ∧ order cap ∧ RSK-04'."""
    all_fail = RiskSnapshot(new_worst_case=D("9999"), buying_power=D("0"),
                            open_worst_cases=(D("99999"),), max_day_risk=D("1"),
                            order_cap_allows_entry=False)
    assert evaluate_risk(all_fail) == "insufficient_bp"


def test_no_buying_power_supplied_means_the_rail_is_off():
    assert _attempt(_condor(), RiskSnapshot(new_worst_case=D("0")))[0].status == "FILLED"


def test_the_day_reads_buying_power_from_its_provider():
    """SIM-04: in paper this provider is the SimLedger; live, derivative BP."""
    events: list = []
    day = _day(events, buying_power=lambda: D("100"))
    filled = asyncio.run(day.run("2026-07-06", [ScheduledEntry(OPEN, _condor(1))]))
    assert filled == 0
    assert "insufficient_bp" in {r for _, r in day_report(events).skips}


# --- ENT-09: a manual fire bypasses the window and NOTHING else ----------------

def test_manual_fire_bypasses_only_the_window():
    """The window is the one rule ENT-09 relaxes."""
    stale = OPEN.replace(hour=10, minute=7)   # clock is at OPEN; scheduled long past
    broker, events, clock = FakeBroker(), [], FakeClock(stale)
    broker.autofill(IS_CONDOR)
    ex = ExecuteEntryAttempt(broker, clock, events, SPX)

    scheduled_miss = asyncio.run(ex.attempt(day="2026-07-06", scheduled=OPEN,
                                            condor=_condor(), gates=_gates()))
    assert (scheduled_miss.status, scheduled_miss.reason) == ("SKIPPED", "missed_window")

    manual = asyncio.run(ex.attempt(day="2026-07-06", scheduled=OPEN, condor=_condor(2),
                                    gates=_gates(), bypass_window=True))
    assert manual.status == "FILLED"


def test_manual_fire_is_still_vetoed_by_rsk04():
    """TC-ENT-08 scenario 4: 'RSK-04 vetoes a manual entry like any other'."""
    risk = RiskSnapshot(new_worst_case=D("0"), open_worst_cases=(D("6000"),),
                        max_day_risk=D("10000"))
    outcome, broker, _ = _attempt(_condor(), risk, bypass_window=True)
    assert (outcome.status, outcome.reason) == ("SKIPPED", "max_day_risk")
    assert asyncio.run(broker.working_orders()) == []


def test_manual_fire_is_still_refused_when_stop_trading_is_on():
    """TC-ENT-08 scenario 3: gates are never bypassed."""
    broker, events, clock = FakeBroker(), [], FakeClock(OPEN)
    ex = ExecuteEntryAttempt(broker, clock, events, SPX)
    blocked = GateSnapshot(armed=True, confirm_live=True, stop_trading=True,
                           flatten_in_progress=False, market_open=True, market_halted=False,
                           data_fresh=True, session_valid=True, buying_power_ok=True)
    outcome = asyncio.run(ex.attempt(day="2026-07-06", scheduled=OPEN, condor=_condor(),
                                     gates=blocked, bypass_window=True))
    assert (outcome.status, outcome.reason) == ("SKIPPED", "stop_trading")


# --- the day scheduler feeds the rails -----------------------------------------

def _day(events, *, max_day_risk=None, order_cap=None, buying_power=None):
    state = PersistentState(InMemoryStateStore())
    state.entry_schedule = [{"time": "x"}] * 3
    state.armed = True
    state.confirm_live = True
    broker = FakeBroker()
    broker.autofill(IS_CONDOR)
    clock = FakeClock(OPEN)
    return RunTradingDay(clock, state, ExecuteEntryAttempt(broker, clock, events, SPX),
                         events, max_day_risk=max_day_risk, order_cap=order_cap,
                         buying_power=buying_power)


def test_the_day_accumulates_open_worst_cases_and_stops_entering():
    """Each 4600 fill eats headroom; the third entry is vetoed, not attempted."""
    events: list = []
    day = _day(events, max_day_risk=D("10000"))     # room for exactly two 4600s
    schedule = [ScheduledEntry(OPEN, _condor(i + 1)) for i in range(3)]

    filled = asyncio.run(day.run("2026-07-06", schedule))

    assert filled == 2
    assert "max_day_risk" in {r for _, r in day_report(events).skips}


def test_a_closed_entry_stops_counting_against_max_day_risk():
    """RSK-04 is about OPEN condors — a closed one can no longer lose anything."""
    from meic.domain.events import EntryClosed

    events: list = []
    day = _day(events, max_day_risk=D("10000"))
    asyncio.run(day.run("2026-07-06", [ScheduledEntry(OPEN, _condor(1)),
                                       ScheduledEntry(OPEN, _condor(2))]))
    assert day_report(events).entries_filled == 2      # 9200 of 10000 used

    events.append(EntryClosed(entry_id="2026-07-06#1", initiator="take_profit"))
    risk = day._risk("2026-07-06")
    assert risk.open_worst_cases == (D("4600"),)       # only #2 still on the books


def test_the_day_respects_the_rsk08_order_cap():
    events: list = []
    day = _day(events, order_cap=OrderCap(cap=1, buffer=1, count=0))  # no headroom
    filled = asyncio.run(day.run("2026-07-06", [ScheduledEntry(OPEN, _condor(1))]))
    assert filled == 0
    assert "order_cap" in {r for _, r in day_report(events).skips}
