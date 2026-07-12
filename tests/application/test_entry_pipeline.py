"""ExecuteEntryAttempt + ENT-03 gates — unit tests (ENT-02/03, ORD-02/03)."""
import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal as D

from meic.application.entry_gates import GateSnapshot, evaluate_gates
from meic.application.execute_entry import Condor, ExecuteEntryAttempt, within_window
from meic.domain.events import CondorFilled, EntrySkipped, FilledLeg
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


async def _drive(clock, coro):
    """Run an attempt() that POLLS the clock for a live fill, advancing the FakeClock
    so the reprice-interval gaps actually elapse. The live gap is real (v-2026-07-09
    incident #2 fix); tests simulate its passage rather than block forever."""
    task = asyncio.ensure_future(coro)
    for _ in range(2000):
        if task.done():
            break
        for _ in range(6):
            await asyncio.sleep(0)   # let pending awaits resolve to the next clock waiter
        clock.advance(seconds=5)     # nudge time forward to release the reprice wait
    return await task


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
    out = asyncio.run(_drive(clock, _svc(broker, events, clock, entry_reprice_attempts=5).attempt(
        day="d", scheduled=SCHEDULED, condor=CONDOR, gates=PASS)))
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


# --- BUG 1 (2026-07-09 incident): record the BROKER-ACTUAL net fill credit ----

def test_record_fill_uses_broker_allocated_prices_when_all_present():
    """The incident: the ladder's last rung was 3.50, but the broker's per-leg
    allocations netted 3.60 (sold 1.80+1.95, bought 0.08+0.07) and the bot
    recorded the rung's 3.50. ORD-09/STP-02d: the allocated prices are the
    source of truth for what was actually paid/received whenever every leg
    carries one."""
    broker, events = FakeBroker(), []
    broker.script_submit(Scripted("fill"))

    async def fake_fill_legs(order_id):
        return (
            FilledLeg(symbol="SPXW260709P07535000", right="P", role="short", qty=1, price=D("1.80")),
            FilledLeg(symbol="SPXW260709P07510000", right="P", role="long", qty=1, price=D("0.08")),
            FilledLeg(symbol="SPXW260709C07540000", right="C", role="short", qty=1, price=D("1.95")),
            FilledLeg(symbol="SPXW260709C07565000", right="C", role="long", qty=1, price=D("0.07")),
        )
    broker.fill_legs = fake_fill_legs

    condor = Condor(1, D("7535"), D("7540"), D("1.85"), D("2.00"), D("3.50"), D("2.00"),
                    put_long=D("7510"), call_long=D("7565"), expiration=date(2026, 7, 9))
    clock = FakeClock(SCHEDULED)
    out = asyncio.run(_svc(broker, events, clock).attempt(
        day="d", scheduled=SCHEDULED, condor=condor, gates=PASS))

    assert out.status == "FILLED"
    assert out.fill_credit == D("3.60")   # actual, not the 3.50 rung
    filled = [e for e in events if isinstance(e, CondorFilled)]
    assert len(filled) == 1
    assert filled[0].net_credit == D("3.60")
    assert filled[0].at is not None   # FEATURE 1: fill time stamped


def test_record_fill_falls_back_to_rung_price_when_any_leg_price_is_missing():
    """The honest paper/missing-allocation case: FakeBroker's simulated fills
    always report price=None, so this is also today's default suite behaviour —
    asserted explicitly so a future change can't silently start fabricating a
    number STP-02d exists to reconcile against."""
    broker, events = FakeBroker(), []
    broker.script_submit(Scripted("fill", payload={"net_credit": "4.00"}))
    clock = FakeClock(SCHEDULED)
    out = asyncio.run(_svc(broker, events, clock).attempt(
        day="d", scheduled=SCHEDULED, condor=CONDOR, gates=PASS))

    assert out.status == "FILLED" and out.fill_credit == D("4.00")  # the mid rung
    filled = [e for e in events if isinstance(e, CondorFilled)]
    assert filled[0].net_credit == D("4.00")
    assert all(leg.price is None for leg in filled[0].legs)


def test_fill_matches_handles_paper_dicts_and_live_order_objects():
    """Regression (2026-07-09 naked position): the paper SimulatedBroker/FakeBroker
    yield fill records as DICTS, but the live TastytradeAdapter yields SDK order
    OBJECTS (.id/.status, NO .get). Every pipeline test used the dict shape, so
    _filled crashed on the object shape in production — AFTER the condor filled,
    BEFORE stops were placed. _fill_matches must accept both."""
    from types import SimpleNamespace
    from meic.application.execute_entry import _fill_matches

    # paper shape: dict
    assert _fill_matches({"order_id": "O-1"}, "O-1") is True
    assert _fill_matches({"order_id": "O-1", "partial": True}, "O-1") is False
    assert _fill_matches({"order_id": "O-2"}, "O-1") is False

    # live shape: an SDK order object with no .get()
    filled = SimpleNamespace(id=482314017, status="Filled")
    assert _fill_matches(filled, "482314017") is True          # matches by .id, not .get
    assert _fill_matches(filled, "999") is False
    partial = SimpleNamespace(id=482314017, status="Partially Filled")
    assert _fill_matches(partial, "482314017") is False        # partial is not filled
