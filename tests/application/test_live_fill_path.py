"""End-to-end LIVE-shaped entry -> fill -> stops.

This is the coverage that was MISSING on 2026-07-09, when three live-only bugs
(object shapes x2, zero reprice gap) each left a real condor unprotected. It runs
the real ExecuteEntryAttempt + ProtectPosition against a broker with SDK object
shapes AND fill latency, so any of those regressions re-breaks a test here.
"""
import asyncio
from datetime import date, datetime
from decimal import Decimal as D

import pytest

from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.leg_book import LegBook
from meic.application.protect_position import ProtectPosition, ShortLeg
from meic.domain.events import CondorFilled, StopConfirmed, StopPlaced
from meic.domain.stop_policy import StopBasis
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_clock import ET, FakeClock
from tests.harness.live_broker import LiveShapedBroker

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
SCHEDULED = datetime(2026, 7, 9, 12, 0, tzinfo=ET)
PASS = GateSnapshot(armed=True, confirm_live=True, stop_trading=False, flatten_in_progress=False,
                    market_open=True, market_halted=False, data_fresh=True, session_valid=True,
                    buying_power_ok=True)
CONDOR = Condor(entry_number=1, put_short=D("7525"), call_short=D("7550"),
                put_short_mid=D("1.50"), call_short_mid=D("2.00"),
                mid_credit=D("4.00"), min_total_credit=D("2.00"),
                put_long=D("7505"), call_long=D("7570"),
                expiration=date(2026, 7, 9), contracts=1)
ENTRY_ID = "2026-07-09#1"


class _Alerts:
    def __init__(self):
        self.calls = []

    def alert(self, level, message, **ctx):
        self.calls.append((level, message))


async def _drive(clock, coro):
    """Run the attempt while advancing the FakeClock, so the fill-latency window and
    the reprice gap actually pass (both are real waits in the fixed code)."""
    task = asyncio.ensure_future(coro)
    for _ in range(3000):
        if task.done():
            break
        for _ in range(6):
            await asyncio.sleep(0)
        clock.advance(seconds=1)
    return await task


def test_live_shaped_fill_places_and_confirms_both_stops():
    clock = FakeClock(SCHEDULED)
    broker = LiveShapedBroker(clock, fill_delay=3.0)   # fills 3s after submit (LATENCY)
    events: list = []
    ex = ExecuteEntryAttempt(broker, clock, events, SPX)

    outcome = asyncio.run(_drive(clock, ex.attempt(
        day="2026-07-09", scheduled=SCHEDULED, condor=CONDOR, gates=PASS)))

    # (incident #2) FILLED on the FIRST order — the ladder waited for the async fill
    # instead of repricing it into a duplicate. Exactly ONE entry order was sent.
    assert outcome.status == "FILLED", outcome
    entry_submits = [s for s in broker.submits if s[1] == "limit"]
    assert len(entry_submits) == 1, f"entry order submitted {len(entry_submits)}x (duplicate!)"
    assert sum(isinstance(e, CondorFilled) for e in events) == 1

    # (incident #1) the fill's broker legs were recorded (object-shaped fill_legs),
    # so the two shorts are known and stops can name them.
    shorts_recorded = LegBook.from_events(events).shorts(ENTRY_ID)
    assert len(shorts_recorded) == 2

    # now protect: place a stop on each short and CONFIRM it (object-shaped
    # working_orders keyed by `.id` — the second latent bug).
    mids = {"PUT": CONDOR.put_short_mid, "CALL": CONDOR.call_short_mid}
    shorts = [ShortLeg(l.side, mids[l.side], D("0.50"), symbol=l.symbol) for l in shorts_recorded]
    protect = ProtectPosition(broker, clock, _Alerts(), events, SPX)
    result = asyncio.run(protect.protect(
        entry_id=ENTRY_ID, basis=StopBasis.TOTAL_CREDIT, shorts=shorts,
        pct=D("95"), total_net_credit=CONDOR.mid_credit, contracts=1))

    assert result.outcome == "PROTECTED"
    assert sum(isinstance(e, StopPlaced) for e in events) == 2      # both sides placed
    assert sum(isinstance(e, StopConfirmed) for e in events) == 2   # both sides CONFIRMED


def test_harness_rejects_repricing_a_filled_order():
    """The live broker rejects a replace of an already-filled order — the real
    margin_check_failed on the duplicate. This is the condition the reprice fix must
    avoid; the end-to-end test above proves it does (exactly one entry submit)."""
    from meic.application.order_intent import OrderIntent, condor_legs

    clock = FakeClock(SCHEDULED)
    broker = LiveShapedBroker(clock, fill_delay=1.0)
    intent = OrderIntent(order_type="limit", tif="Day", kind="iron_condor", entry_id=ENTRY_ID,
                         contracts=1, price=D("4.00"), underlying="SPXW", expiration=date(2026, 7, 9),
                         idempotency_key="e", legs=condor_legs(
                             put_short=D("7525"), put_long=D("7505"),
                             call_short=D("7550"), call_long=D("7570"), contracts=1))
    oid = asyncio.run(broker.submit(intent))
    clock.advance(seconds=2)  # let it fill
    with pytest.raises(RuntimeError, match="margin_check_failed"):
        asyncio.run(broker.replace(oid, intent))
