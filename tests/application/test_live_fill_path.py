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


# --- ENT-10(3): a cancel mid-LADDER must never orphan a working entry order -----

def test_cancel_at_floor_racing_a_fill_is_recorded_filled_not_skipped():
    """REPRICE-RACE SWEEP (2026-07-11): the ORD-03 cancel-at-floor guard
    re-confirms not-filled BEFORE cancel() — but a live fill can still land IN
    the cancel() round trip itself (neither adapter's cancel() reliably
    reports "already filled" back to the caller). Arms LiveShapedBroker's
    race hook so the entry fills exactly during that cancel() call and proves
    the post-cancel re-check catches it: FILLED, not the naked, invisible
    `unfilled_at_floor` skip a pre-check-only guard would silently produce."""
    from dataclasses import replace as dc_replace

    clock = FakeClock(SCHEDULED)
    broker = LiveShapedBroker(clock, fill_delay=10_000.0)  # never fills "naturally"
    events: list = []
    condor = dc_replace(CONDOR, mid_credit=D("4.00"), min_total_credit=D("4.00"))  # exactly one rung
    ex = ExecuteEntryAttempt(broker, clock, events, SPX,
                             entry_reprice_seconds=2, entry_reprice_attempts=1)

    async def scenario():
        task = asyncio.ensure_future(ex.attempt(
            day="2026-07-09", scheduled=SCHEDULED, condor=condor, gates=PASS))
        for _ in range(500):
            await asyncio.sleep(0)
            if broker.submits:
                break
        assert broker.submits, "the attempt never reached the broker"
        oid = broker.submits[0][0]
        broker.race_fill_on_cancel(oid)  # the fill lands DURING the coming cancel()
        for _ in range(200):
            if task.done():
                break
            for _ in range(6):
                await asyncio.sleep(0)
            clock.advance(seconds=1)
        return await task

    outcome = asyncio.run(scenario())
    assert outcome.status == "FILLED", outcome
    assert sum(isinstance(e, CondorFilled) for e in events) == 1
    entry_submits = [s for s in broker.submits if s[1] == "limit"]
    assert len(entry_submits) == 1, f"entry order submitted {len(entry_submits)}x (duplicate!)"


def test_stop_confirmation_miss_adopts_resting_stop_instead_of_resubmitting():
    """REPRICE-RACE SWEEP (2026-07-11): a stop submit can succeed at the
    broker even though ITS OWN confirmation read misses it (a slow/
    eventually-consistent working_orders() read — the same shape as the
    historical `.order_id`-vs-`.id` bug, 2026-07-09). tastytrade enforces no
    server-side idempotency key, so a caller that blindly resubmits on the
    next retry rests a genuine SECOND stop. `hide_from_working_orders` forces
    exactly that miss on the first confirmation read; the fix (ProtectPosition
    adopting a resting order before resubmitting) must still confirm PROTECTED
    with only ONE stop ever submitted."""
    clock = FakeClock(SCHEDULED)
    broker = LiveShapedBroker(clock, fill_delay=10_000.0)
    events: list = []
    protect = ProtectPosition(broker, clock, _Alerts(), events, SPX, stop_retry_attempts=3)

    shorts = [ShortLeg("PUT", D("1.50"), D("0"), symbol="SPXW_7525P")]

    orig_submit = broker.submit

    async def submit_and_hide(intent):
        oid = await orig_submit(intent)
        if intent.order_type == "stop_market":
            broker.hide_from_working_orders(oid, times=1)  # miss the FIRST confirm read
        return oid

    broker.submit = submit_and_hide  # type: ignore[method-assign]

    result = asyncio.run(protect.protect(
        entry_id="e-adopt", basis=StopBasis.TOTAL_CREDIT, shorts=shorts,
        pct=D("95"), total_net_credit=D("3.00"), contracts=1))

    assert result.outcome == "PROTECTED", result
    stop_submits = [s for s in broker.submits if s[1] == "stop_market"]
    assert len(stop_submits) == 1, f"stop submitted {len(stop_submits)}x (duplicate!)"
    assert sum(isinstance(e, StopPlaced) for e in events) == 1
    assert sum(isinstance(e, StopConfirmed) for e in events) == 1


def test_replace_race_mid_ladder_is_recorded_filled_not_raised():
    """REPRICE-RACE SWEEP (2026-07-11): the pre-replace `_filled()` check
    narrows the reprice race but does not close it — a fill can still land in
    the gap between that check and the replace() call itself, which the real
    broker rejects (margin_check_failed on the duplicate). Before this fix
    that exception propagated uncaught out of the ladder; now it must be
    recognized as the race it is and recorded as a fill."""
    clock = FakeClock(SCHEDULED)
    broker = LiveShapedBroker(clock, fill_delay=10_000.0)  # never fills "naturally"
    events: list = []
    ex = ExecuteEntryAttempt(broker, clock, events, SPX,
                             entry_reprice_seconds=2, entry_reprice_attempts=3)

    async def scenario():
        task = asyncio.ensure_future(ex.attempt(
            day="2026-07-09", scheduled=SCHEDULED, condor=CONDOR, gates=PASS))
        for _ in range(500):
            await asyncio.sleep(0)
            if broker.submits:
                break
        assert broker.submits, "the attempt never reached the broker"
        oid = broker.submits[0][0]
        # arm the race on the SECOND rung's replace() call, which the ladder
        # is about to make once the first rung's poll window times out
        broker.race_fill_on_replace(oid)
        for _ in range(200):
            if task.done():
                break
            for _ in range(6):
                await asyncio.sleep(0)
            clock.advance(seconds=1)
        return await task

    outcome = asyncio.run(scenario())
    assert outcome.status == "FILLED", outcome
    assert sum(isinstance(e, CondorFilled) for e in events) == 1


def test_cancel_mid_ladder_never_orphans_a_working_entry_order():
    """Re-review finding (2026-07-09): a disarm cancel landing INSIDE the attempt
    — during the reprice ladder's submit/_await_fill polls — used to unwind with
    the entry limit order still WORKING at the broker, unwatched. If it filled
    later there'd be a real position with no CondorFilled, no stop, no alert.
    The whole attempt is now an atomic shielded unit: the outer day task dies
    instantly, the attempt runs to its natural end and protects its own fill."""
    from meic.adapters.persistence.event_store import InMemoryStateStore
    from meic.application.persistent_state import PersistentState
    from meic.composition.live_runtime import LiveRuntime, ScheduledRow

    async def scenario():
        clock = FakeClock(SCHEDULED)
        broker = LiveShapedBroker(clock, fill_delay=3.0)   # fills 3s after submit
        events: list = []
        protected: list[str] = []

        comp = type("Comp", (), {})()
        comp.clock = clock
        comp.broker = broker
        comp.events = events
        comp.state = PersistentState(InMemoryStateStore())
        comp.state.armed = True
        comp.state.confirm_live = True
        comp.state.stop_trading = False
        comp.execute = ExecuteEntryAttempt(broker, clock, events, SPX)

        async def on_filled(entry_id, condor, stop=None, fill_credit=None):
            protected.append(entry_id)
        comp._on_filled = on_filled

        async def selector(when, n, config=None):
            return CONDOR, None

        async def gates():
            return PASS

        rt = LiveRuntime(comp, selector=selector, market_gates=gates)
        day_task = asyncio.create_task(
            rt.run_day("2026-07-09", [ScheduledRow(SCHEDULED, number=1)]))

        for _ in range(500):        # let the attempt SUBMIT; the clock does NOT
            await asyncio.sleep(0)  # advance, so the order cannot fill yet —
            if broker.submits:      # the attempt is genuinely mid-ladder
                break
        assert broker.submits, "the attempt never reached the broker"
        assert not any(isinstance(e, CondorFilled) for e in events)   # pre-fill

        day_task.cancel()           # ENT-10(3): the disarm/stop path
        with pytest.raises(asyncio.CancelledError):
            await day_task          # the day task dies instantly — desired

        # drive the clock so the SHIELDED attempt runs to its natural end
        for _ in range(60):
            for _ in range(6):
                await asyncio.sleep(0)
            clock.advance(seconds=1)

        # the attempt finished: the fill was RECORDED and PROTECTED...
        assert sum(isinstance(e, CondorFilled) for e in events) == 1
        assert protected == [ENTRY_ID]
        # ...and the broker holds NO orphaned working entry order
        working = await broker.working_orders()
        assert working == [], f"orphaned working orders: {[o.id for o in working]}"

    asyncio.run(scenario())
