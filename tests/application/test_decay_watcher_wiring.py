"""DCY-01..04 decay-watcher WIRING (2026-07-14, NFR-07 pinned regression) --
`DecayWatcher` (application/decay_watcher.py) was fully written, unit-tested
(tests/application/test_tpf_dcy.py) and race-guarded
(test_decay_watcher_live_shaped.py, whose own docstring flagged it as unwired)
but never constructed, ticked, or wired into the live app -- grep confirmed
zero `DecayWatcher(` hits anywhere under backend/src outside the module
itself.

These tests pin the WIRING seam (`_decay_watcher_pass`/`_run_decay_watcher_loop`
in adapters/api/server.py): feeding the watcher LIVE QuoteHub marks off the
SAME `_open_short_legs`/`_streamer_symbol` frames the stop watchdog
(test_watchdog_wiring.py) and the stop-fill catch-up loop already use, routing
the buyback/re-inflation guard through the real `DecayWatcher` methods, and
supervising it exactly like every other live background task (never crashes
the app). The pure decision logic itself (confirmation-eval counting,
gate_allows' matrix, the buyback/reinflation-guard procedures) is
`DecayWatcher`'s own job and already covered in test_tpf_dcy.py; these tests
prove the WIRING drives it correctly and honestly, not a second copy of that
logic.
"""
import asyncio
from datetime import datetime, timedelta
from datetime import time as dtime
from decimal import Decimal as D

from meic.adapters.api.server import _decay_watcher_pass, _run_decay_watcher_loop
from meic.application.decay_watcher import DecayWatcher
from meic.application.order_intent import protective_stop
from meic.domain.events import CondorFilled, DecayBuybackPlaced, FilledLeg, StopPlaced
from meic.domain.staleness import StampedQuote
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import ET

ENTRY, SIDE = "e1", "PUT"
SHORT_SYM = "SPXW  260714P07535000"   # ORD-09 broker form (double space)
LONG_SYM = "SPXW  260714P07510000"
STREAMER = ".SPXW260714P7535"
TRIGGER = D("3.80")

NOW = datetime(2026, 7, 14, 15, 0, tzinfo=ET)


class _FakeSnap:
    def __init__(self, streamer_symbols):
        self.streamer_symbols = streamer_symbols


class _Snaps:
    def __init__(self, last):
        self.last = last


class _FakeHub:
    def __init__(self, marks: dict | None = None):
        self.marks = marks or {}

    def mark(self, symbol):
        return self.marks.get(symbol)


class _Alerts:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def alert(self, level, message, **ctx):
        self.calls.append((level, message, ctx))


class _State:
    def __init__(self, stop_trading: bool = False):
        self.stop_trading = stop_trading


class _Comp:
    def __init__(self, events, broker, stop_trading: bool = False):
        self.events = events
        self.broker = broker
        self.state = _State(stop_trading)


def _snap():
    return _FakeSnap({D("7535"): (STREAMER, ".SPXW260714C7535")})


def _condor_filled():
    return CondorFilled(entry_id=ENTRY, net_credit=D("3.60"), legs=(
        FilledLeg(symbol=SHORT_SYM, right="P", role="short", qty=1, price=D("1.80")),
        FilledLeg(symbol=LONG_SYM, right="P", role="long", qty=1, price=D("0.08")),
    ))


def _stop_placed(events, broker) -> str:
    resting_id = asyncio.run(broker.submit(protective_stop(
        entry_id=ENTRY, right="P", contracts=1, trigger=TRIGGER, symbol=SHORT_SYM,
        idempotency_key=f"stop:{ENTRY}:{SIDE}")))
    events.append(StopPlaced(entry_id=ENTRY, side=SIDE, trigger=TRIGGER, broker_order_id=resting_id))
    return resting_id


def _quote(ask: str, *, at: datetime = NOW) -> StampedQuote:
    return StampedQuote(STREAMER, D(ask), D(ask), at)


def _pass(comp, watchers, active, hub, alerts, *, now, flatten=False, suspended=None,
          enabled=True, confirmation_evals=2, unfilled_timeout=D("30"),
          cutoff=dtime(15, 55)):
    if suspended is None:
        suspended = {"value": False}
    asyncio.run(_decay_watcher_pass(
        comp, watchers, active, hub, _Snaps(_snap()), alerts, now=now,
        max_quote_age_ms=3000, buyback_trigger=D("0.05"), confirmation_evals=confirmation_evals,
        unfilled_timeout_seconds=unfilled_timeout, cutoff_time=cutoff, enabled=enabled,
        fee_model=None, clock=None, flatten_in_progress=lambda: flatten, suspended=suspended))
    return suspended


# --- DCY-01: two confirmation evals fire the buyback --------------------------

def test_pass_fires_buyback_after_two_confirmation_evals_via_live_hub_marks():
    events = [_condor_filled()]
    broker = FakeBroker()
    resting_id = _stop_placed(events, broker)
    comp = _Comp(events, broker)
    hub = _FakeHub({STREAMER: _quote("0.05", at=NOW)})
    alerts = _Alerts()
    watchers: dict = {}
    active: dict = {}

    _pass(comp, watchers, active, hub, alerts, now=NOW)
    assert not any(isinstance(e, DecayBuybackPlaced) for e in events), "one eval must not fire"
    assert (ENTRY, SIDE) not in active

    t2 = NOW + timedelta(seconds=5)
    hub.marks[STREAMER] = _quote("0.05", at=t2)
    _pass(comp, watchers, active, hub, alerts, now=t2)

    placed = [e for e in events if isinstance(e, DecayBuybackPlaced)]
    assert len(placed) == 1, "the second consecutive valid eval must fire the buyback"
    assert placed[0].entry_id == ENTRY and placed[0].side == SIDE
    assert (ENTRY, SIDE) in active
    assert broker._orders[resting_id].status == "CANCELLED", "DCY-02(1): the resting stop must be cancelled"
    assert isinstance(watchers[(ENTRY, SIDE)], DecayWatcher)


def test_a_single_bad_print_resets_the_confirmation_counter():
    events = [_condor_filled()]
    broker = FakeBroker()
    _stop_placed(events, broker)
    comp = _Comp(events, broker)
    hub = _FakeHub({STREAMER: _quote("0.05", at=NOW)})
    alerts = _Alerts()
    watchers: dict = {}
    active: dict = {}

    _pass(comp, watchers, active, hub, alerts, now=NOW)
    t2 = NOW + timedelta(seconds=5)
    hub.marks[STREAMER] = _quote("0.20", at=t2)  # recovers above trigger
    _pass(comp, watchers, active, hub, alerts, now=t2)
    t3 = t2 + timedelta(seconds=5)
    hub.marks[STREAMER] = _quote("0.05", at=t3)  # restarts the count at 1
    _pass(comp, watchers, active, hub, alerts, now=t3)

    assert not any(isinstance(e, DecayBuybackPlaced) for e in events)


def test_an_absent_or_stale_mark_is_treated_as_stale_never_a_guessed_zero():
    """NFR-04: a decay decision must come off a LIVE QuoteHub mark, not a
    fabricated zero -- an absent hub tick must reset the counter exactly like
    an aged one, never be silently treated as ask=0 (which is <= any trigger
    and would fire on nothing at all)."""
    events = [_condor_filled()]
    broker = FakeBroker()
    _stop_placed(events, broker)
    comp = _Comp(events, broker)
    hub = _FakeHub({})   # nothing subscribed yet
    alerts = _Alerts()
    watchers: dict = {}
    active: dict = {}

    _pass(comp, watchers, active, hub, alerts, now=NOW)
    t2 = NOW + timedelta(seconds=5)
    _pass(comp, watchers, active, hub, alerts, now=t2)

    assert not any(isinstance(e, DecayBuybackPlaced) for e in events)


# --- ORD-08: the resting stop already filled -> abort, no double buy --------

def test_stop_already_filled_aborts_to_lex_and_submits_no_buyback():
    events = [_condor_filled()]
    broker = FakeBroker()
    resting_id = _stop_placed(events, broker)
    broker._orders[resting_id].status = "FILLED"  # it was a real stop-out
    comp = _Comp(events, broker)
    hub = _FakeHub({STREAMER: _quote("0.05", at=NOW)})
    alerts = _Alerts()
    watchers: dict = {}
    active: dict = {}

    _pass(comp, watchers, active, hub, alerts, now=NOW)
    t2 = NOW + timedelta(seconds=5)
    hub.marks[STREAMER] = _quote("0.05", at=t2)
    _pass(comp, watchers, active, hub, alerts, now=t2)

    assert not any(isinstance(e, DecayBuybackPlaced) for e in events)
    assert (ENTRY, SIDE) not in active
    limit_orders = [o for o in broker._orders.values() if o.intent.order_type == "limit"]
    assert limit_orders == [], "ORD-08: a filled resting stop must never get a second buy-to-close"


# --- DCY-02(3): the re-inflation guard --------------------------------------

def _placed_buyback(now=NOW):
    events = [_condor_filled()]
    broker = FakeBroker()
    _stop_placed(events, broker)
    comp = _Comp(events, broker)
    hub = _FakeHub({STREAMER: _quote("0.05", at=now)})
    alerts = _Alerts()
    watchers: dict = {}
    active: dict = {}
    _pass(comp, watchers, active, hub, alerts, now=now)
    t2 = now + timedelta(seconds=5)
    hub.marks[STREAMER] = _quote("0.05", at=t2)
    _pass(comp, watchers, active, hub, alerts, now=t2)
    assert (ENTRY, SIDE) in active
    return comp, hub, alerts, watchers, active, t2


def test_reinflation_guard_reprotects_after_the_unfilled_timeout():
    comp, hub, alerts, watchers, active, placed_at = _placed_buyback()
    t_timeout = placed_at + timedelta(seconds=31)   # > default 30s
    hub.marks[STREAMER] = _quote("0.05", at=t_timeout)  # still at/below trigger, just unfilled
    _pass(comp, watchers, active, hub, alerts, now=t_timeout)

    assert (ENTRY, SIDE) not in active, "a reprotected side must leave the in-flight bookkeeping"
    # the ORIGINAL resting stop is CANCELLED (by the buyback's own cancel) and
    # stays in the fake's order book -- the re-inflation guard's re-place is
    # the one still WORKING.
    working_stops = [o for o in comp.broker._orders.values()
                      if o.intent.order_type == "stop_market" and o.status == "WORKING"]
    assert len(working_stops) == 1, "the re-inflation guard must re-place the resting stop"
    assert working_stops[0].intent.legs[0].right == "P"


def test_reinflation_guard_reprotect_journals_the_new_stop_so_fill_detection_can_see_it():
    """Review finding (2026-07-14, BLOCKING): the re-placed stop's own broker
    order id was being silently discarded -- `_stop_specs` (stop_fill_watch.py,
    latest-journaled-StopPlaced-wins) would keep pointing at the OLD, now-
    cancelled stop forever, making a genuine fill on the NEW stop invisible to
    fill detection (REC-02) and to STP-03b's own watchdog. The re-inflation
    guard's REPROTECTED outcome must journal a fresh StopPlaced with the NEW
    id, exactly like the original placement did."""
    from meic.application.stop_fill_watch import _stop_specs

    comp, hub, alerts, watchers, active, placed_at = _placed_buyback()
    old_specs = _stop_specs(comp.events)
    old_stop_id = old_specs[(ENTRY, SIDE)].broker_order_id

    t_timeout = placed_at + timedelta(seconds=31)
    hub.marks[STREAMER] = _quote("0.05", at=t_timeout)
    _pass(comp, watchers, active, hub, alerts, now=t_timeout)

    working_stops = [o for o in comp.broker._orders.values()
                      if o.intent.order_type == "stop_market" and o.status == "WORKING"]
    assert len(working_stops) == 1
    new_stop_id = working_stops[0].order_id
    assert new_stop_id != old_stop_id

    new_specs = _stop_specs(comp.events)
    assert new_specs[(ENTRY, SIDE)].broker_order_id == new_stop_id, (
        "the journal must point at the NEW resting stop, never the stale cancelled one")
    assert new_specs[(ENTRY, SIDE)].trigger == TRIGGER


def test_reinflation_guard_never_touches_the_broker_when_a_concurrent_close_already_resolved_it():
    """Review finding (2026-07-14, BLOCKING): `reinflation_guard`'s own race
    check only looks at `fills_since` for the buyback id -- it has no way to
    see that a CONCURRENT close (manual/TPF/TPT/EOD -- none of which set
    `flatten_in_progress`) already REPLACED this exact buyback order via
    CloseEntry's own race-safe path. Cancelling an already-replaced order and
    submitting a fresh stop on top of a leg CloseEntry just closed would rest
    a PHANTOM stop on a flat leg. The pass must re-confirm the buyback is
    STILL genuinely resting at the broker before touching it at all; if some
    other actor already resolved it, drop the bookkeeping and submit NOTHING."""
    comp, hub, alerts, watchers, active, placed_at = _placed_buyback()
    buyback_id = active[(ENTRY, SIDE)]["buyback_id"]
    # Simulate a concurrent CloseEntry.close() having already REPLACED the
    # buyback (broker.replace()'s real outcome -- see FakeBroker.replace()).
    comp.broker._orders[buyback_id].status = "REPLACED"

    t_timeout = placed_at + timedelta(seconds=31)
    hub.marks[STREAMER] = _quote("0.05", at=t_timeout)
    _pass(comp, watchers, active, hub, alerts, now=t_timeout)

    assert (ENTRY, SIDE) not in active, "bookkeeping must be dropped once another actor resolved it"
    stop_market_orders = [o for o in comp.broker._orders.values()
                           if o.intent.order_type == "stop_market"]
    # only the ORIGINAL resting stop (now cancelled by the buyback's own
    # placement) exists -- no phantom re-placed stop was submitted.
    assert len(stop_market_orders) == 1, (
        "no phantom stop may be submitted once the buyback was already resolved elsewhere")


def test_reinflation_guard_reprotects_when_the_ask_rises_above_trigger():
    comp, hub, alerts, watchers, active, placed_at = _placed_buyback()
    t2 = placed_at + timedelta(seconds=2)   # well before the timeout
    hub.marks[STREAMER] = _quote("0.30", at=t2)   # the ask jumped back up
    _pass(comp, watchers, active, hub, alerts, now=t2)

    assert (ENTRY, SIDE) not in active
    working_stops = [o for o in comp.broker._orders.values()
                      if o.intent.order_type == "stop_market" and o.status == "WORKING"]
    assert len(working_stops) == 1


def test_reinflation_guard_leaves_a_still_live_buyback_alone():
    comp, hub, alerts, watchers, active, placed_at = _placed_buyback()
    t2 = placed_at + timedelta(seconds=2)
    hub.marks[STREAMER] = _quote("0.05", at=t2)   # still at/below trigger, well inside the timeout
    _pass(comp, watchers, active, hub, alerts, now=t2)

    assert (ENTRY, SIDE) in active, "a genuinely still-live buyback must not be touched"
    stop_orders = [o for o in comp.broker._orders.values() if o.intent.order_type == "stop_market"]
    assert len(stop_orders) == 1, "only the ORIGINAL resting stop -- no re-inflation re-place yet"


def test_reinflation_guard_stands_down_during_a_flatten_leaving_close_entry_to_resolve_it():
    """DCY-01 'never while a Flatten All is executing' covers the WHOLE
    watcher, not just a fresh trigger -- the guard must not race a
    concurrent CloseEntry.close() replace() over the same buyback order
    (close_assembly.py now folds a working decay buyback into
    resting_stop_ids so that close resolves it through its own race-safe
    path)."""
    comp, hub, alerts, watchers, active, placed_at = _placed_buyback()
    t_timeout = placed_at + timedelta(seconds=31)   # would otherwise trigger the guard
    hub.marks[STREAMER] = _quote("0.05", at=t_timeout)
    _pass(comp, watchers, active, hub, alerts, now=t_timeout, flatten=True)

    assert (ENTRY, SIDE) in active, "flatten_in_progress must leave the in-flight buyback untouched"
    working_stops = [o for o in comp.broker._orders.values()
                      if o.intent.order_type == "stop_market" and o.status == "WORKING"]
    assert working_stops == [], "the guard must not re-place a stop while a flatten is executing"


def test_reinflation_guard_failure_suspends_the_watcher_under_stop_trading_and_clears_on_reset():
    """DCY-01's last sentence: a re-inflation re-placement failure while
    Stop Trading is active suspends the watcher for the remainder of that
    Stop Trading state; the suspension clears once Stop Trading resets."""
    comp, hub, alerts, watchers, active, placed_at = _placed_buyback()
    comp.state.stop_trading = True

    # The in-flight DecayWatcher instance (in `watchers`) already bound
    # `comp.broker` at construction time -- exactly like production, where a
    # DecayWatcher holds its own broker reference, not a live lookup off
    # `comp`. Fail the SAME broker's cancel() (the re-inflation guard's own
    # call) rather than swapping `comp.broker`, which would silently miss the
    # already-bound reference and never actually exercise this path.
    async def _failing_cancel(order_id):
        raise RuntimeError("broker down")
    comp.broker.cancel = _failing_cancel
    suspended = {"value": False}
    t_timeout = placed_at + timedelta(seconds=31)
    hub.marks[STREAMER] = _quote("0.05", at=t_timeout)
    _pass(comp, watchers, active, hub, alerts, now=t_timeout, suspended=suspended)

    assert suspended["value"] is True
    assert any(level == "critical" for level, _msg, _ctx in alerts.calls)
    assert (ENTRY, SIDE) in active, "the guard failure must leave the in-flight bookkeeping alone"

    # a fresh tick (fresh comp/broker -- a NEW tracked short) with stop_trading
    # still True stays suspended -- the suspension is watcher-global, not tied
    # to the specific side that triggered it.
    events2 = [_condor_filled()]
    broker2 = FakeBroker()
    _stop_placed(events2, broker2)
    comp2 = _Comp(events2, broker2, stop_trading=True)
    hub2 = _FakeHub({STREAMER: _quote("0.05", at=NOW)})
    watchers2: dict = {}
    active2: dict = {}
    _pass(comp2, watchers2, active2, hub2, alerts, now=NOW, suspended=suspended)
    t2 = NOW + timedelta(seconds=5)
    hub2.marks[STREAMER] = _quote("0.05", at=t2)
    _pass(comp2, watchers2, active2, hub2, alerts, now=t2, suspended=suspended)
    assert not any(isinstance(e, DecayBuybackPlaced) for e in events2), (
        "DCY-01: a suspended watcher must not fire a fresh buyback")

    # stop_trading clears -> the suspension is lifted "for the remainder of the
    # stop-trading state" -- a fresh (non-stop-trading) state starts unsuspended.
    comp2.state.stop_trading = False
    t3 = t2 + timedelta(seconds=5)
    hub2.marks[STREAMER] = _quote("0.05", at=t3)
    _pass(comp2, watchers2, active2, hub2, alerts, now=t3, suspended=suspended)
    assert suspended["value"] is False


# --- DCY-01 gates: cutoff time, flatten-in-progress, enabled -----------------

def test_cutoff_time_blocks_a_new_trigger():
    events = [_condor_filled()]
    broker = FakeBroker()
    _stop_placed(events, broker)
    comp = _Comp(events, broker)
    at = NOW.replace(hour=15, minute=56)   # at/after the default 15:55 cutoff
    hub = _FakeHub({STREAMER: _quote("0.05", at=at)})
    alerts = _Alerts()
    watchers: dict = {}
    active: dict = {}

    _pass(comp, watchers, active, hub, alerts, now=at)
    t2 = at + timedelta(seconds=5)
    hub.marks[STREAMER] = _quote("0.05", at=t2)
    _pass(comp, watchers, active, hub, alerts, now=t2)

    assert not any(isinstance(e, DecayBuybackPlaced) for e in events)


def test_flatten_in_progress_blocks_a_new_trigger():
    events = [_condor_filled()]
    broker = FakeBroker()
    _stop_placed(events, broker)
    comp = _Comp(events, broker)
    hub = _FakeHub({STREAMER: _quote("0.05", at=NOW)})
    alerts = _Alerts()
    watchers: dict = {}
    active: dict = {}

    _pass(comp, watchers, active, hub, alerts, now=NOW, flatten=True)
    t2 = NOW + timedelta(seconds=5)
    hub.marks[STREAMER] = _quote("0.05", at=t2)
    _pass(comp, watchers, active, hub, alerts, now=t2, flatten=True)

    assert not any(isinstance(e, DecayBuybackPlaced) for e in events)


def test_disabled_config_never_evaluates_anything():
    events = [_condor_filled()]
    broker = FakeBroker()
    _stop_placed(events, broker)
    comp = _Comp(events, broker)
    hub = _FakeHub({STREAMER: _quote("0.05", at=NOW)})
    alerts = _Alerts()
    watchers: dict = {}
    active: dict = {}

    _pass(comp, watchers, active, hub, alerts, now=NOW, enabled=False)
    t2 = NOW + timedelta(seconds=5)
    hub.marks[STREAMER] = _quote("0.05", at=t2)
    _pass(comp, watchers, active, hub, alerts, now=t2, enabled=False)

    assert not any(isinstance(e, DecayBuybackPlaced) for e in events)
    assert watchers == {} and active == {}


# --- the loop never crashes the app ------------------------------------------

class _ExplodingHub:
    def mark(self, symbol):
        raise RuntimeError("hub lookup blew up")


def test_run_decay_watcher_loop_swallows_a_pass_failure_and_keeps_running():
    events = [_condor_filled()]
    broker = FakeBroker()
    _stop_placed(events, broker)
    comp = _Comp(events, broker)
    alerts = _Alerts()

    class _Clock:
        def now(self):
            return NOW

    async def _drive():
        task = asyncio.create_task(_run_decay_watcher_loop(
            comp, _ExplodingHub(), _Snaps(_snap()), alerts, clock=_Clock(),
            max_quote_age_ms=3000, buyback_trigger=D("0.05"), confirmation_evals=2,
            unfilled_timeout_seconds=D("30"), cutoff_time=dtime(15, 55), enabled=True,
            fee_model=None, flatten_in_progress=lambda: False,
            idle_seconds=0.01, connected=lambda: True))
        try:
            await asyncio.wait_for(task, timeout=0.1)
        except asyncio.TimeoutError:
            pass
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_drive())

    assert len(alerts.calls) >= 1
    assert all(level == "warning" for level, _msg, _ctx in alerts.calls)
    assert all("decay watcher" in msg for _level, msg, _ctx in alerts.calls)


def test_run_decay_watcher_loop_idles_without_touching_the_hub_when_not_connected():
    events = [_condor_filled()]
    broker = FakeBroker()
    _stop_placed(events, broker)
    comp = _Comp(events, broker)
    alerts = _Alerts()

    class _Clock:
        def now(self):
            return NOW

    async def _drive():
        task = asyncio.create_task(_run_decay_watcher_loop(
            comp, _ExplodingHub(), _Snaps(_snap()), alerts, clock=_Clock(),
            max_quote_age_ms=3000, buyback_trigger=D("0.05"), confirmation_evals=2,
            unfilled_timeout_seconds=D("30"), cutoff_time=dtime(15, 55), enabled=True,
            fee_model=None, flatten_in_progress=lambda: False,
            idle_seconds=0.01, connected=lambda: False))
        try:
            await asyncio.wait_for(task, timeout=0.05)
        except asyncio.TimeoutError:
            pass
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_drive())

    assert alerts.calls == []   # never touched the (exploding) hub -- nothing failed


def test_run_decay_watcher_loop_exposes_its_bookkeeping_to_caller_supplied_dicts():
    """The loop must mutate the CALLER's dicts (as `live_app()` passes
    `app.state.decay_watchers`/`app.state.decay_watcher_active`), never a
    private copy nobody outside the loop's closure can observe -- otherwise
    the wiring registry's 'ticked' proof would be decorative."""
    events = [_condor_filled()]
    broker = FakeBroker()
    _stop_placed(events, broker)
    comp = _Comp(events, broker)
    hub = _FakeHub({STREAMER: _quote("0.05", at=NOW)})
    alerts = _Alerts()
    watchers: dict = {}
    active: dict = {}

    class _Clock:
        def __init__(self):
            self.t = NOW

        def now(self):
            return self.t

    clock = _Clock()

    async def _drive():
        task = asyncio.create_task(_run_decay_watcher_loop(
            comp, hub, _Snaps(_snap()), alerts, clock=clock,
            max_quote_age_ms=3000, buyback_trigger=D("0.05"), confirmation_evals=2,
            unfilled_timeout_seconds=D("30"), cutoff_time=dtime(15, 55), enabled=True,
            fee_model=None, flatten_in_progress=lambda: False,
            idle_seconds=0.01, connected=lambda: True,
            watchers=watchers, active=active))
        await asyncio.sleep(0.02)
        clock.t = NOW + timedelta(seconds=5)
        hub.marks[STREAMER] = _quote("0.05", at=clock.t)
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_drive())

    assert (ENTRY, SIDE) in watchers, "the caller-supplied dict must be the one actually ticked"
    assert isinstance(watchers[(ENTRY, SIDE)], DecayWatcher)
