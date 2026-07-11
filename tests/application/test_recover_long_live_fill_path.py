"""LIVE-shaped RecoverLong ladder — the incident-#2 class, now on the LEX side.

Mirrors test_live_fill_path.py's proof for the entry ladder. Before the fix,
RecoverLong's ladder had no clock, no real wait between rungs, and its own
hand-rolled `_filled()` did raw `dict.get(...)` on a fill record — so a live
sell fill that registered mid-rung was either repriced into a duplicate
(margin_check_failed) or crashed the whole ladder outright. This drives the
SAME LiveShapedBroker (SDK object shapes + fill latency + reject-on-replace-
after-fill) through RecoverLong specifically.
"""
import asyncio
from datetime import datetime
from decimal import Decimal as D

import pytest

from meic.application.recover_long import Quote, RecoverLong
from meic.domain.events import LongSold, SideClosed
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_clock import ET, FakeClock
from tests.harness.live_broker import LiveShapedBroker

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
SCHEDULED = datetime(2026, 7, 9, 12, 0, tzinfo=ET)


async def _drive(clock, coro):
    """Run the ladder while advancing the FakeClock, so the fill-latency window
    and the reprice gap actually pass (both are real waits in the fixed code)."""
    task = asyncio.ensure_future(coro)
    for _ in range(3000):
        if task.done():
            break
        for _ in range(6):
            await asyncio.sleep(0)
        clock.advance(seconds=1)
    return await task


def test_live_shaped_lex_fill_mid_rung_never_duplicates():
    """The LEX sell fills 3s after submit (LATENCY, like a real broker). The
    ladder's own reprice_seconds is 5s, so the fill lands mid-rung — exactly
    the race incident #2 was about. Fixed: the ladder polls for the fill and
    stops; exactly ONE LEX order is ever submitted."""
    clock = FakeClock(SCHEDULED)
    broker = LiveShapedBroker(clock, fill_delay=3.0)
    events: list = []
    rec = RecoverLong(broker, clock, events, SPX, lex_reprice_seconds=5, lex_fill_poll_seconds=1.0)

    outcome = asyncio.run(_drive(clock, rec.recover(
        entry_id="e1", side="PUT", long_symbol="SPXW_5940P",
        quote=Quote(bid=D("2.00"), ask=D("2.30")), intrinsic=D("0"))))

    assert outcome.outcome == "SOLD", outcome
    lex_submits = [s for s in broker.submits if s[1] == "limit"]
    assert len(lex_submits) == 1, f"LEX order submitted {len(lex_submits)}x (duplicate!)"
    assert sum(isinstance(e, LongSold) for e in events) == 1
    assert sum(isinstance(e, SideClosed) for e in events) == 1


def test_replace_race_mid_ladder_is_recorded_sold_not_raised():
    """REPRICE-RACE SWEEP (2026-07-11): the pre-replace `_filled()` check
    narrows the LEX reprice race but does not close it — a sell fill can
    still land in the gap between that check and the replace() call itself
    (margin_check_failed on the duplicate, the real broker's rejection).
    Before this fix that exception propagated uncaught out of the ladder;
    now it must be recognized as the race it is and recorded SOLD."""
    clock = FakeClock(SCHEDULED)
    broker = LiveShapedBroker(clock, fill_delay=10_000.0)  # never fills "naturally"
    events: list = []
    rec = RecoverLong(broker, clock, events, SPX, lex_reprice_seconds=2,
                      lex_reprice_attempts=3, lex_fill_poll_seconds=0.5)

    async def scenario():
        task = asyncio.ensure_future(rec.recover(
            entry_id="e1", side="PUT", long_symbol="SPXW_5940P",
            quote=Quote(bid=D("2.00"), ask=D("2.30")), intrinsic=D("0")))
        for _ in range(500):
            await asyncio.sleep(0)
            if broker.submits:
                break
        assert broker.submits, "the ladder never reached the broker"
        oid = broker.submits[0][0]
        broker.race_fill_on_replace(oid)
        for _ in range(200):
            if task.done():
                break
            for _ in range(6):
                await asyncio.sleep(0)
            clock.advance(seconds=1)
        return await task

    outcome = asyncio.run(scenario())
    assert outcome.outcome == "SOLD", outcome
    assert sum(isinstance(e, LongSold) for e in events) == 1


def test_harness_rejects_repricing_an_already_filled_lex_order():
    """Sanity check on the harness itself (mirrors test_live_fill_path.py's
    twin): replacing an already-filled LEX order raises margin_check_failed,
    the real broker's behaviour. This is the condition the fix above must
    never hit; the test above proves it doesn't."""
    from meic.application.order_intent import OrderIntent, OrderLeg, right_of

    clock = FakeClock(SCHEDULED)
    broker = LiveShapedBroker(clock, fill_delay=1.0)
    intent = OrderIntent(order_type="limit", tif="Day", kind="lex", entry_id="e1",
                         contracts=1, price=D("2.15"), idempotency_key="lex:e1:PUT",
                         legs=(OrderLeg(right=right_of("PUT"), action="sell_to_close",
                                       qty=1, symbol="SPXW_5940P"),))
    oid = asyncio.run(broker.submit(intent))
    clock.advance(seconds=2)  # let it fill
    with pytest.raises(RuntimeError, match="margin_check_failed"):
        asyncio.run(broker.replace(oid, intent))
