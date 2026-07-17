"""ORD-04/EC-API-03 (2026-07-17 security review finding A) — the entry
ladder's FIRST submit must never treat a client-side exception as "no order
exists" without asking the broker first.

Before this fix, `execute_entry._run_ladder`'s first-rung `await
self._broker.submit(intent)` had no try/except: a lost-response-after-commit
(the broker accepted the order, the client never saw the ack -- an ordinary
home-network failure) propagated straight out of the attempt. In production
that crashes the fire-and-forget attempt task; `attempt_crash.py`'s callback
finds no `CondorFilled` and journals `EntrySkipped(reason="attempt_crashed:
...")` plus an alert saying "no position was taken" — while a live, STOPLESS
4-leg condor rests at the broker, invisible until the next reboot.

These tests pin the three outcomes of the fix:
  1. the broker CONFIRMS the order landed -> ADOPT it, the ladder continues,
     the condor still gets its stop (fail-first: pre-fix this path raised
     straight out of `attempt()` and never adopted anything).
  2. the broker CONFIRMS it did NOT land -> today's exact clean-skip path
     (the exception still propagates unchanged) -- no regression.
  3. the query ITSELF is inconclusive -> never a silent "no position taken"
     skip; a critical alert names the entry and says a position MAY be live
     and unprotected.
  4. no double-submit: the recovery path only ever QUERIES, never resubmits.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal as D

import pytest

from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.leg_book import LegBook
from meic.application.protect_position import ProtectPosition, ShortLeg
from meic.domain.events import CondorFilled, EntrySkipped, StopConfirmed, StopPlaced
from meic.domain.stop_policy import StopBasis
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker, Scripted
from tests.harness.fake_clock import ET, FakeClock
from tests.harness.live_broker import LiveShapedBroker

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
SCHEDULED = datetime(2026, 7, 17, 10, 0, tzinfo=ET)
PASS = GateSnapshot(armed=True, confirm_live=True, stop_trading=False, flatten_in_progress=False,
                    market_open=True, market_halted=False, data_fresh=True, session_valid=True,
                    buying_power_ok=True)
CONDOR = Condor(1, D("5990"), D("6060"), D("3.00"), D("2.00"), D("4.00"), D("2.00"))


class _Alerts:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def alert(self, level, message, **context) -> None:
        self.calls.append((level, message, context))


def _skips(events):
    return [e for e in events if isinstance(e, EntrySkipped)]


def _fills(events):
    return [e for e in events if isinstance(e, CondorFilled)]


# --- 1. lost-ack, order LANDED -> ADOPT, no phantom skip -----------------------

def test_lost_ack_with_order_landed_adopts_instead_of_crashing():
    """FAIL-FIRST scenario: submit() raises a client timeout, but the order
    actually landed at the broker (FakeBroker's `lost_ack` scripts exactly
    that: the order is created WORKING/queryable, then submit() raises).
    Pre-fix, this propagated straight out of attempt() -- the fire-and-forget
    task would crash and the callback would wrongly claim "no position was
    taken". Post-fix: the broker query finds the landed order, adopts it,
    and the ladder finishes the fill normally."""
    broker, events = FakeBroker(), []
    broker.script_submit(Scripted("lost_ack", payload={"net_credit": "4.00"}))
    clock = FakeClock(SCHEDULED)
    alerts = _Alerts()

    out = asyncio.run(ExecuteEntryAttempt(broker, clock, events, SPX, alerts=alerts).attempt(
        day="d", scheduled=SCHEDULED, condor=CONDOR, gates=PASS))

    assert out.status == "FILLED" and out.fill_credit == D("4.00")
    assert len(_fills(events)) == 1
    assert _skips(events) == []                # never a phantom skip
    assert alerts.calls == []                  # a clean adopt needs no alert
    # NO DOUBLE-SUBMIT: exactly one submit() call ever reached the broker.
    assert len(broker._orders) == 1


def test_lost_ack_adoption_lets_the_condor_get_its_stop_end_to_end():
    """THE named scenario, end-to-end: submit raises a client timeout BUT the
    order is present at the broker => the bot adopts it, the ladder fills,
    and ProtectPosition (STP-01) places + confirms a resting stop on each
    short -- StopPlaced is journaled, exactly as if submit() had never
    raised. Uses the SDK-object-shaped LiveShapedBroker (fill latency, `.id`
    shapes) so this exercises the real live-shaped path, not just paper's
    synchronous fills."""
    condor = Condor(entry_number=1, put_short=D("7525"), call_short=D("7550"),
                    put_short_mid=D("1.50"), call_short_mid=D("2.00"),
                    mid_credit=D("4.00"), min_total_credit=D("2.00"),
                    put_long=D("7505"), call_long=D("7570"),
                    expiration=date(2026, 7, 17), contracts=1)
    entry_id = "2026-07-17#1"

    clock = FakeClock(SCHEDULED)
    broker = LiveShapedBroker(clock, fill_delay=3.0)
    broker.lose_ack_on_submit(1)   # the FIRST submit's ack is lost; the order still lands
    events: list = []
    ex = ExecuteEntryAttempt(broker, clock, events, SPX)

    async def _drive(coro):
        task = asyncio.ensure_future(coro)
        for _ in range(3000):
            if task.done():
                break
            for _ in range(6):
                await asyncio.sleep(0)
            clock.advance(seconds=1)
        return await task

    outcome = asyncio.run(_drive(ex.attempt(
        day="2026-07-17", scheduled=SCHEDULED, condor=condor, gates=PASS)))

    assert outcome.status == "FILLED", outcome
    assert sum(isinstance(e, CondorFilled) for e in events) == 1
    # exactly ONE entry submit ever happened -- the lost ack did not cause a
    # second (duplicate) order.
    entry_submits = [s for s in broker.submits if s[1] == "limit"]
    assert len(entry_submits) == 1, f"entry order submitted {len(entry_submits)}x (duplicate!)"

    shorts_recorded = LegBook.from_events(events).shorts(entry_id)
    assert len(shorts_recorded) == 2
    mids = {"PUT": condor.put_short_mid, "CALL": condor.call_short_mid}
    shorts = [ShortLeg(l.side, mids[l.side], D("0.50"), symbol=l.symbol) for l in shorts_recorded]
    protect = ProtectPosition(broker, clock, _Alerts(), events, SPX)
    result = asyncio.run(protect.protect(
        entry_id=entry_id, basis=StopBasis.TOTAL_CREDIT, shorts=shorts,
        pct=D("95"), total_net_credit=condor.mid_credit, contracts=1))

    assert result.outcome == "PROTECTED"
    assert sum(isinstance(e, StopPlaced) for e in events) == 2      # the condor GOT its stop
    assert sum(isinstance(e, StopConfirmed) for e in events) == 2


# --- 2. genuine failure, order did NOT land -> clean skip, no regression ------

def test_submit_failure_with_no_order_landed_still_propagates_unchanged():
    """Contrast case: the submit raises and the order genuinely never
    reached the broker (FakeBroker's `timeout` action creates no order at
    all). The query correctly reports "not found", so the ORIGINAL exception
    still propagates -- today's exact clean-skip path (handled upstream by
    attempt_crash.py in production), with no phantom adoption."""
    broker, events = FakeBroker(), []
    broker.script_submit(Scripted("timeout"))
    clock = FakeClock(SCHEDULED)
    alerts = _Alerts()

    async def scenario():
        return await ExecuteEntryAttempt(broker, clock, events, SPX, alerts=alerts).attempt(
            day="d", scheduled=SCHEDULED, condor=CONDOR, gates=PASS)

    with pytest.raises(TimeoutError):
        asyncio.run(scenario())

    assert _fills(events) == []
    assert _skips(events) == []   # this layer never journals here -- the caller's
                                   # crash machinery does, unchanged from before
    assert alerts.calls == []     # a confirmed non-landing needs no NEW alert here
    assert broker._orders == {}   # no order was ever created -- no double-submit risk


# --- 3. INDETERMINATE: the query itself fails -> critical alert, never silent -

def test_indeterminate_query_failure_alerts_critical_never_silently_skips():
    """The dangerous case: submit raises AND the broker query meant to
    resolve it ALSO fails/is ambiguous. Must NEVER be journaled as a clean
    "no position taken" skip -- a CRITICAL alert names the entry and says a
    position MAY be live and unprotected, and the skip reason is distinctly
    labelled (never `attempt_crashed:...`, which -- upstream -- carries the
    misleading "no position was taken" wording)."""
    broker, events = FakeBroker(), []
    broker.script_submit(Scripted("timeout"))

    async def _boom(order):
        raise ConnectionError("no route to broker")
    broker.find_matching_order = _boom  # type: ignore[method-assign]

    clock = FakeClock(SCHEDULED)
    alerts = _Alerts()

    out = asyncio.run(ExecuteEntryAttempt(broker, clock, events, SPX, alerts=alerts).attempt(
        day="d", scheduled=SCHEDULED, condor=CONDOR, gates=PASS))

    assert out.status == "SKIPPED"
    assert out.reason == "submit_indeterminate"
    assert _fills(events) == []

    skips = _skips(events)
    assert len(skips) == 1
    assert skips[0].reason == "submit_indeterminate"

    critical = [c for c in alerts.calls if c[0] == "critical"]
    assert len(critical) == 1
    message = critical[0][1]
    assert "d#1" in message or "entry" in message.lower()
    assert "MAY be resting" in message or "may be" in message.lower()
    assert "no position was taken" not in message   # never the misleading wording
    assert "submit_error" in critical[0][2] and "query_error" in critical[0][2]

    # no double-submit: the query failure must never trigger a resubmission.
    assert broker._orders == {}


# --- 4/5. DISCRIMINATING POWER: adopt the RIGHT order, never a decoy ------------

def test_lost_ack_adopts_our_order_not_a_structurally_identical_decoy():
    """FINDING 4/5 (the discriminating-power gap the review flagged): a DECOY
    order with IDENTICAL legs (same strikes/expiry/contracts) but a DIFFERENT
    entry_id -- hence a different `entry:{entry_id}` idempotency key -- is
    already resting at the broker (the operator's own order, or a different
    entry, on a shared account). It is seeded FIRST, so a leg-shape matcher
    that returns the first structural match would adopt the WRONG order. The
    recovery must adopt only the order carrying OUR key."""
    broker, events = FakeBroker(), []
    clock = FakeClock(SCHEDULED)
    alerts = _Alerts()

    async def scenario():
        # 1) seed the decoy: identical legs, foreign entry_id/key, resting WORKING
        from meic.application.order_intent import OrderIntent, condor_legs
        decoy = OrderIntent(
            order_type="limit", tif="Day", contracts=1, kind="iron_condor",
            underlying="SPXW", expiration=date(2026, 7, 17), price=D("4.00"), entry_id="OTHER#99",
            idempotency_key="entry:OTHER#99",
            legs=condor_legs(put_short=D("5990"), put_long=D("5940"),
                             call_short=D("6060"), call_long=D("6110"), contracts=1))
        broker.script_submit(Scripted("work"))     # the decoy just rests
        decoy_id = await broker.submit(decoy)

        # 2) now the real entry: submit's ack is lost but the order lands + fills
        broker.script_submit(Scripted("lost_ack", payload={"net_credit": "4.00"}))
        out = await ExecuteEntryAttempt(broker, clock, events, SPX, alerts=alerts).attempt(
            day="d", scheduled=SCHEDULED, condor=CONDOR, gates=PASS)
        return out, decoy_id

    out, decoy_id = asyncio.run(scenario())

    assert out.status == "FILLED"
    filled = _fills(events)
    assert len(filled) == 1
    # the adopted order is OURS (key entry:d#1), never the decoy
    assert filled[0].broker_order_id != decoy_id
    adopted = filled[0].broker_order_id
    assert broker._orders[adopted].intent.idempotency_key == "entry:d#1"
    assert broker._orders[decoy_id].status == "WORKING"   # decoy untouched, still resting
    assert alerts.calls == []


def test_lost_ack_with_only_a_cancelled_same_key_order_present_does_not_adopt():
    """FINDING 2/3 end-to-end: a prior unfilled_at_floor entry left a
    CANCELLED order reusing `entry:d#1`, and THIS attempt's submit genuinely
    never lands. The recovery query finds the cancelled order by key but the
    status filter refuses it -- so the original exception propagates (clean
    skip), never a phantom adoption of a dead order."""
    broker, events = FakeBroker(), []
    clock = FakeClock(SCHEDULED)
    alerts = _Alerts()

    async def scenario():
        from meic.application.order_intent import OrderIntent, condor_legs
        stale = OrderIntent(
            order_type="limit", tif="Day", contracts=1, kind="iron_condor",
            underlying="SPXW", expiration=date(2026, 7, 17), price=D("4.00"), entry_id="d#1",
            idempotency_key="entry:d#1",   # SAME key a fresh d#1 attempt will use
            legs=condor_legs(put_short=D("5990"), put_long=D("5940"),
                             call_short=D("6060"), call_long=D("6110"), contracts=1))
        broker.script_submit(Scripted("work"))
        stale_id = await broker.submit(stale)
        await broker.cancel(stale_id)   # it is now CANCELLED at the broker

        # the fresh attempt's submit genuinely fails (order never lands)
        broker.script_submit(Scripted("timeout"))
        with pytest.raises(TimeoutError):
            await ExecuteEntryAttempt(broker, clock, events, SPX, alerts=alerts).attempt(
                day="d", scheduled=SCHEDULED, condor=CONDOR, gates=PASS)
        return stale_id

    stale_id = asyncio.run(scenario())

    assert _fills(events) == []               # the cancelled order was NOT adopted
    assert alerts.calls == []                 # confirmed-not-landed => no NEW alert here
    assert broker._orders[stale_id].status == "CANCELLED"
