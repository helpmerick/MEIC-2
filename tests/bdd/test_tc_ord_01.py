"""Hand-written step definitions for TC-ORD-01 — the entry reprice ladder
(ORD-02/03, EC-ENT-05). Drives the real ExecuteEntryAttempt against a
never-filling broker and asserts the price walk, the floor, and the skip."""
import asyncio
from datetime import datetime
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.domain.events import EntrySkipped
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import ET, FakeClock

scenarios("../features/TC-ORD-01.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
SCHEDULED = datetime(2026, 7, 6, 10, 0, 0, tzinfo=ET)
GATES_PASS = GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                          flatten_in_progress=False, market_open=True, market_halted=False,
                          data_fresh=True, session_valid=True, buying_power_ok=True)
CONDOR = Condor(entry_number=1, put_short=D("5990"), call_short=D("6060"),
                put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                mid_credit=D("4.00"), min_total_credit=D("2.00"))


class RecordingBroker(FakeBroker):
    """A never-filling broker that records every credit price it was asked to
    work, and every cancel."""

    def __init__(self):
        super().__init__()
        self.prices: list[D] = []
        self.cancels: list[str] = []

    async def submit(self, order):
        if order.kind == "iron_condor":
            self.prices.append(order.price)
        return await super().submit(order)

    async def cancel(self, id):
        self.cancels.append(id)
        return await super().cancel(id)


@pytest.fixture
def world():
    return {}


@given('the entry order does not fill')
def _(world):
    world["broker"] = RecordingBroker()  # default FakeBroker never fills
    world["clock"] = FakeClock(SCHEDULED)


async def _run_advancing(clock, coro):
    """Drive the ladder by advancing the FakeClock so each entry_reprice_seconds
    gap elapses. The gap is a REAL wait since the 2026-07-09 incident-#2 fix (a
    live fill needs a beat to register before the ladder may reprice), so 'elapses
    5 times' is simulated here by moving time forward rather than a no-op."""
    task = asyncio.ensure_future(coro)
    for _ in range(2000):
        if task.done():
            break
        for _ in range(6):
            await asyncio.sleep(0)   # let pending awaits reach the next clock waiter
        clock.advance(seconds=5)
    return await task


@when('entry_reprice_seconds elapses 5 times')
def _(world):
    events: list = []
    svc = ExecuteEntryAttempt(world["broker"], world["clock"], events, SPX,
                              entry_reprice_attempts=5)
    world["events"] = events
    world["outcome"] = asyncio.run(_run_advancing(world["clock"], svc.attempt(
        day="2026-07-06", scheduled=SCHEDULED, condor=CONDOR, gates=GATES_PASS)))


@then('the limit was repriced down one tick each time')
def _(world):
    prices = world["broker"].prices
    assert len(prices) == 6                              # start + 5 repricings
    assert prices[0] == CONDOR.mid_credit               # first worked at mid
    for hi, lo in zip(prices, prices[1:]):
        assert lo == hi - SPX.tick_for(hi)               # exactly one tick down each step


@then('never below min_total_credit')
def _(world):
    assert all(p >= CONDOR.min_total_credit for p in world["broker"].prices)


@then('after the final attempt the order is cancelled and entry SKIPPED "unfilled_at_floor"')
def _(world):
    assert world["broker"].cancels                       # the resting order was cancelled
    assert world["outcome"].status == "SKIPPED"
    assert world["outcome"].reason == "unfilled_at_floor"
    assert any(isinstance(e, EntrySkipped) and e.reason == "unfilled_at_floor"
               for e in world["events"])
