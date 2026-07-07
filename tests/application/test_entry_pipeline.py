"""ExecuteEntryAttempt + ENT-03 gates — unit tests (ENT-02/03, ORD-02/03)."""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal as D

from meic.application.entry_gates import GateSnapshot, evaluate_gates
from meic.application.execute_entry import Condor, ExecuteEntryAttempt, within_window
from meic.domain.events import CondorFilled, EntrySkipped
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker, Scripted
from tests.harness.fake_clock import ET, FakeClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
SCHEDULED = datetime(2026, 7, 6, 10, 0, tzinfo=ET)
PASS = GateSnapshot(armed=True, confirm_live=True, stop_trading=False, flatten_in_progress=False,
                    market_open=True, market_halted=False, data_fresh=True, session_valid=True,
                    buying_power_ok=True)
CONDOR = Condor(1, D("5990"), D("6060"), D("3.00"), D("2.00"), D("4.00"), D("2.00"))


def _svc(broker, events, clock, **kw):
    return ExecuteEntryAttempt(broker, clock, events, SPX, **kw)


def test_gate_order_first_failure_wins():
    # armed off AND stop trading on -> disarmed reported first (ENT-03 order)
    s = GateSnapshot(armed=False, confirm_live=True, stop_trading=True, flatten_in_progress=False,
                     market_open=True, market_halted=False, data_fresh=True, session_valid=True,
                     buying_power_ok=True)
    assert evaluate_gates(s) == "disarmed"
    assert evaluate_gates(PASS) is None


def test_within_window_boundaries():
    assert within_window(SCHEDULED, SCHEDULED, 120)
    assert within_window(SCHEDULED + timedelta(seconds=120), SCHEDULED, 120)
    assert not within_window(SCHEDULED + timedelta(seconds=121), SCHEDULED, 120)
    assert not within_window(SCHEDULED - timedelta(seconds=1), SCHEDULED, 120)


def test_fills_at_mid_first_rung():
    broker, events = FakeBroker(), []
    broker.script_submit(Scripted("fill", payload={"net_credit": "4.00"}))
    clock = FakeClock(SCHEDULED)
    out = asyncio.run(_svc(broker, events, clock).attempt(
        day="d", scheduled=SCHEDULED, condor=CONDOR, gates=PASS))
    assert out.status == "FILLED" and out.fill_credit == D("4.00")
    assert sum(isinstance(e, CondorFilled) for e in events) == 1


def test_ord03_floor_reached_unfilled_cancels_and_skips():
    broker, events = FakeBroker(), []  # default: every submit stays WORKING (never fills)
    clock = FakeClock(SCHEDULED)
    out = asyncio.run(_svc(broker, events, clock, entry_reprice_attempts=5).attempt(
        day="d", scheduled=SCHEDULED, condor=CONDOR, gates=PASS))
    assert out.status == "SKIPPED" and out.reason == "unfilled_at_floor"  # EC-ENT-05
    assert any(isinstance(e, EntrySkipped) for e in events)


def test_gate_failure_skips_before_any_order():
    broker, events = FakeBroker(), []
    stop_gates = GateSnapshot(**{**PASS.__dict__, "stop_trading": True})
    clock = FakeClock(SCHEDULED)
    out = asyncio.run(_svc(broker, events, clock).attempt(
        day="d", scheduled=SCHEDULED, condor=CONDOR, gates=stop_gates))
    assert out.status == "SKIPPED" and out.reason == "stop_trading"
    assert broker._orders == {}
