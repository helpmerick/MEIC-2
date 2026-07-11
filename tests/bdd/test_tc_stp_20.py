"""Hand-written step definitions for TC-STP-20 — STP-08a's live stop-fill
reaction chain (v1.61). Drives the REAL modules the live composition wires:

  * `order_event_watch.py` — the push consumer (`consume_order_events`), the
    asymmetric lock helpers (`run_pass_locked` / `run_pass_if_idle`), and the
    outage lifecycle (capped backoff, one alert per outage, resumption).
  * `stop_fill_watch.py` — the ONE decision path both wake sources funnel
    into (`detect_and_recover_stop_fills`): it reads broker truth + the
    journal on every call and acts idempotently.

Reuses the unit-test harnesses directly (tests/application/
test_stop_fill_watch.py's `_FakeBroker`/`_RecoverSpy`/`_Alerts`/`_Comp` and
test_order_event_watch.py's live-shaped `_filled_event`), so these steps bind
the identical construction the unit suites already pin — never a parallel
re-implementation.

Scenario 5 pins the NEW v1.61 behaviour: a fill identified (by the journaled
`DecayBuybackPlaced.broker_order_id`) as the DCY buyback classifies
SIDE_CLOSED_DECAY — journaled with decay_watcher.complete()'s exact shape
(ShortStopped initiator="decay" + EntryClosed initiator="decay") — the long is
left to expire (DCY-03) and no LEX ladder ever starts.
"""
import asyncio
import contextlib
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.order_event_watch import (
    consume_order_events,
    run_pass_if_idle,
    run_pass_locked,
)
from meic.application.stop_fill_watch import detect_and_recover_stop_fills
from meic.domain.events import (
    DecayBuybackPlaced,
    EntryClosed,
    LongSaleStarted,
    LongSold,
    ShortStopped,
    SideClosed,
    StopPlaced,
)
from tests.application.test_order_event_watch import _filled_event
from tests.application.test_stop_fill_watch import (
    CALL_LONG,
    CALL_SHORT,
    ENTRY_ID,
    _Alerts,
    _Comp,
    _condor_filled_events,
    _FakeBroker,
    _quote_provider,
    _RecoverSpy,
)

scenarios("../features/TC-STP-20.feature")


@pytest.fixture
def world():
    return {}


class _LadderSpy(_RecoverSpy):
    """A `_RecoverSpy` that behaves like the real RecoverLong one step further:
    when the ladder starts it RESTS a sell-to-close rung at the broker. A
    later wake for the same side then takes the REAL double-ladder guard
    (`_sell_still_working`) — exactly-once comes from the production guard
    path, not from the spy pretending the side went terminal."""

    def __init__(self, broker):
        super().__init__()
        self._broker = broker

    async def recover(self, **kw):
        await super().recover(**kw)
        self._broker.working.append({
            "order_id": f"LEX-{len(self.calls)}",
            "legs": [{"symbol": kw["long_symbol"], "action": "sell_to_close"}]})


def _stop_fill_world(world):
    """The 2026-07-10 shape both wake sources must resolve identically: an
    open CALL short whose resting stop traded through at the broker while the
    journal still shows the side open (symbol-fallback era StopPlaced)."""
    events = list(_condor_filled_events())
    events.append(StopPlaced(entry_id=ENTRY_ID, side="CALL", trigger=D("3.80")))
    broker = _FakeBroker()
    broker.fills = [{"order_id": "STOP-9",
                     "legs": [{"symbol": CALL_SHORT, "action": "buy_to_close"}]}]
    broker.fill_legs_by_order["STOP-9"] = ()
    broker.positions_ = [{"symbol": CALL_LONG, "signed_qty": 1}]
    recover, alerts = _LadderSpy(broker), _Alerts()
    world.update(events=events, broker=broker, recover=recover, alerts=alerts,
                 comp=_Comp(broker, events, recover))

    async def run_pass():
        # STP-08a(2): the wake carries NO decision data — this zero-argument
        # callable IS what both wake sources invoke; everything it decides is
        # re-read from broker truth (comp.broker) + the journal (comp.events).
        await detect_and_recover_stop_fills(world["comp"], alerts, _quote_provider)

    world["run_pass"] = run_pass
    return world


async def _never():
    await asyncio.Event().wait()


# --- Scenario: Wakes carry no data and one path decides -------------------------

@given("a push event and a poll tick arrive for the same fill")
def _(world):
    _stop_fill_world(world)

    async def scenario():
        lock = asyncio.Lock()   # the ONE app.state.stop_fill_lock, shared by both

        async def order_events():
            yield _filled_event("STOP-9")   # the push wake for the same broker fill
            await _never()

        # PUSH: the supervised consumer reacts to the terminal-filled event by
        # running the (locked) pass — exactly server.py's wiring.
        push = asyncio.create_task(
            consume_order_events(order_events, world["run_pass"], lock, world["alerts"]))
        for _ in range(200):
            if any(isinstance(e, ShortStopped) for e in world["events"]):
                break
            await asyncio.sleep(0.01)
        # POLL: the fallback tick for the SAME fill — same lock, same pass.
        world["poll_ran"] = await run_pass_if_idle(lock, world["run_pass"])
        push.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await push

    asyncio.run(scenario())


@then("exactly one decision path reads broker truth and the journal and acts once")
def _(world):
    # Both wakes funnelled into the SAME detect_and_recover_stop_fills pass
    # (world["run_pass"] — a zero-argument callable: the wake carried no
    # decision data, STP-08a(2)). Acting once = one journaled stop-out and one
    # LEX hand-off, despite two wakes.
    stopped = [e for e in world["events"] if isinstance(e, ShortStopped)]
    assert len(stopped) == 1 and stopped[0].side == "CALL"
    assert len(world["recover"].calls) == 1


@then("the fill is processed exactly once regardless of wake source")
def _(world):
    # The poll tick genuinely RAN the pass after the push (lock free again) —
    # and still nothing doubled: the pass is journal-terminal-aware, so the
    # second wake was a no-op, not a second dialect of fill handling.
    assert world["poll_ran"] is True
    assert len([e for e in world["events"] if isinstance(e, ShortStopped)]) == 1
    assert len(world["recover"].calls) == 1


# --- Scenario: A sold long is never re-sold --------------------------------------

@given("the journal shows the side's long already sold")
def _(world):
    _stop_fill_world(world)
    # The side ran its full course on an earlier wake: stop-out, ladder, sold,
    # terminal. STP-08a(5): journal-terminal awareness.
    world["events"].append(ShortStopped(entry_id=ENTRY_ID, side="CALL",
                                        fill=D("3.85"), slippage=D("0.05")))
    world["events"].append(LongSold(entry_id=ENTRY_ID, side="CALL", recovery=D("0.40")))
    world["events"].append(SideClosed(entry_id=ENTRY_ID, side="CALL"))
    world["len_before"] = len(world["events"])


@when("any wake detects the historical stop fill again")
def _(world):
    # The broker's fills feed still contains the historical stop fill — a
    # push wake and a poll wake both re-run the pass over it.
    async def scenario():
        lock = asyncio.Lock()
        await run_pass_locked(lock, world["run_pass"])     # push-shaped wake
        await run_pass_if_idle(lock, world["run_pass"])    # poll-shaped wake

    asyncio.run(scenario())


@then("no order is placed and the wake is a no-op")
def _(world):
    assert world["recover"].calls == [], "a SOLD LONG IS NEVER RE-SOLD (STP-08a(5))"
    assert len(world["events"]) == world["len_before"], \
        "a terminal side is never re-processed — the wake journals nothing"


# --- Scenario: Poll skips when busy, push waits -----------------------------------

@given("the decision path is mid-action")
def _(world):
    async def scenario():
        lock = asyncio.Lock()
        entered = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []

        async def slow_pass():
            order.append("first")
            entered.set()
            await release.wait()

        first = asyncio.create_task(run_pass_locked(lock, slow_pass))
        await asyncio.wait_for(entered.wait(), timeout=1.0)  # mid-action, lock held

        # POLL tick lands now: SKIPS outright, never queues (asymmetric lock).
        async def poll_pass():
            order.append("poll")

        poll_ran = await asyncio.wait_for(run_pass_if_idle(lock, poll_pass), timeout=0.2)

        # PUSH lands now: WAITS for the lock and still runs afterwards.
        async def push_pass():
            order.append("push")

        push = asyncio.create_task(run_pass_locked(lock, push_pass))
        await asyncio.sleep(0.05)
        assert "push" not in order, "the push must be WAITING, not dropped or run early"
        release.set()
        await first
        await asyncio.wait_for(push, timeout=1.0)

        # the poll's NEXT tick catches up once the lock is free
        poll_ran_next = await run_pass_if_idle(lock, poll_pass)
        world.update(order=order, poll_ran=poll_ran, poll_ran_next=poll_ran_next)

    asyncio.run(scenario())


@then("a poll tick SKIPS (its next tick catches up) and a push WAITS for the lock")
def _(world):
    assert world["poll_ran"] is False, "the busy-tick poll skipped, never queued"
    assert world["order"] == ["first", "push", "poll"], \
        "push queued behind the lock and ran; the poll's NEXT tick caught up"
    assert world["poll_ran_next"] is True


# --- Scenario: Stream outage lifecycle --------------------------------------------

@given("the order-event stream drops")
def _(world):
    _stop_fill_world(world)

    async def scenario():
        lock = asyncio.Lock()
        attempts = {"n": 0}
        sleeps: list[float] = []
        poll_handled = asyncio.Event()

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 4:
                # STP-08a(3): while the stream is down, the fallback poll is
                # AUTHORITATIVE — it runs the same pass and handles the fill.
                world["poll_ran_while_down"] = await run_pass_if_idle(
                    lock, world["run_pass"])
                poll_handled.set()

        async def order_events():
            attempts["n"] += 1
            if attempts["n"] <= 5:
                raise RuntimeError("stream down")
            yield _filled_event("STOP-9")   # resumption re-arms push; wake re-runs the pass
            await _never()

        task = asyncio.create_task(consume_order_events(
            order_events, world["run_pass"], lock, world["alerts"],
            sleep=fake_sleep, max_backoff_s=4.0))
        await asyncio.wait_for(poll_handled.wait(), timeout=2.0)
        for _ in range(200):
            recovered = [c for c in world["alerts"].calls if c[0] == "info"
                         and "recovered" in c[1]]
            if recovered:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        world["sleeps"] = sleeps

    asyncio.run(scenario())


@then("reconnection backs off with a cap, exactly ONE alert fires for the outage")
def _(world):
    # capped exponential backoff: 1, 2, 4, then pinned at max_backoff_s=4.
    assert world["sleeps"][:4] == [1.0, 2.0, 4.0, 4.0]
    down = [c for c in world["alerts"].calls if c[0] == "warning"]
    assert len(down) == 1, "one alert per OUTAGE, never one per retry attempt"


@then("the fallback poll is authoritative until resumption re-arms push")
def _(world):
    # While the stream was down, the poll tick ran the pass and handled the
    # fill (the journal shows the stop-out + hand-off it produced) …
    assert world["poll_ran_while_down"] is True
    assert len([e for e in world["events"] if isinstance(e, ShortStopped)]) == 1
    assert len(world["recover"].calls) == 1
    # … and resumption re-armed push: one recovery alert, and the re-armed
    # push wake (same fill event) was an idempotent no-op, not a double-act.
    recovered = [c for c in world["alerts"].calls if c[0] == "info" and "recovered" in c[1]]
    assert len(recovered) == 1
    assert len([e for e in world["events"] if isinstance(e, ShortStopped)]) == 1
    assert len(world["recover"].calls) == 1


# --- Scenario: A decay buyback fill is never a stop-out ---------------------------

@given("a side's fill is identified as the DCY buyback rather than the stop")
def _(world):
    _stop_fill_world(world)
    # DecayWatcher.buyback() journaled its order id AT PLACEMENT (STP-08a
    # v1.61, DecayBuybackPlaced) — the broker's buy-to-close fill on the
    # short's own symbol matches THAT id, not the stop.
    world["events"].append(DecayBuybackPlaced(
        entry_id=ENTRY_ID, side="CALL", broker_order_id="DCY-1", price=D("0.05")))
    world["broker"].fills = [{"order_id": "DCY-1",
                              "legs": [{"symbol": CALL_SHORT, "action": "buy_to_close"}]}]
    asyncio.run(detect_and_recover_stop_fills(world["comp"], world["alerts"],
                                              _quote_provider))


@then("the side classifies SIDE_CLOSED_DECAY and the long is left to expire")
def _(world):
    # SIDE_CLOSED_DECAY is journaled with decay_watcher.complete()'s exact
    # shape: ShortStopped(initiator="decay") + EntryClosed(initiator="decay"),
    # atomically — never a resting_stop stop-out.
    stopped = [e for e in world["events"] if isinstance(e, ShortStopped)]
    assert len(stopped) == 1 and stopped[0].initiator == "decay"
    assert stopped[0].fill == D("0.05") and stopped[0].slippage == D("0")
    closed = [e for e in world["events"] if isinstance(e, EntryClosed)]
    assert len(closed) == 1 and closed[0].initiator == "decay"
    # DCY-03: the long is left to expire — it is still held at the broker and
    # nothing sold it.
    assert any(p["symbol"] == CALL_LONG for p in world["broker"].positions_)


@then("no LEX ladder starts")
def _(world):
    assert world["recover"].calls == [], "a decay long is never LEX-sold (DCY-03)"
    assert not any(isinstance(e, LongSaleStarted) for e in world["events"])
