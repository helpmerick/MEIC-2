"""order_event_watch.py — ITEM 1 (operator ruling 2026-07-11): "the stop
being hit triggers the long sale immediately; only if that fails does the
periodic check force it." Unit-tests the supervised consumer loop in
isolation (fakes only, no live_app/FastAPI) against live-shaped order
objects (see tests/harness/live_broker.py's `_Order`/`_Leg` shapes, which
mirror what `TastytradeAdapter.order_events()` actually yields:
`{"type": "order_status", "order_id": ..., "status": ..., "raw": <SDK
order>}`).
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from meic.application.order_event_watch import (
    consume_order_events,
    run_pass_if_idle,
    run_pass_locked,
)
from tests.harness.live_broker import _Leg, _Order


class _FakeAlerts:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def alert(self, level: str, message: str, **context) -> None:
        self.calls.append((level, message))


def _filled_event(order_id: str = "1") -> dict:
    """A live-shaped terminal-filled order-status event -- the SAME
    normalized shape TastytradeAdapter.order_events() yields, wrapping an
    SDK-shaped order object (tests/harness/live_broker.py's `_Order`/`_Leg`:
    attributes only, no `.get`)."""
    order = _Order(order_id, "Filled", [_Leg("SPXW  260711P07535000", "Buy to Close", 1)])
    return {"type": "order_status", "order_id": order_id, "status": "filled", "raw": order}


async def _never() -> None:
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_a_terminal_filled_event_triggers_the_pass_once():
    calls: list[bool] = []

    async def run_pass() -> None:
        calls.append(True)

    lock = asyncio.Lock()
    alerts = _FakeAlerts()

    async def order_events():
        yield _filled_event()
        await _never()   # keep the stream open; no more events this test cares about

    task = asyncio.create_task(consume_order_events(order_events, run_pass, lock, alerts))
    for _ in range(200):
        if calls:
            break
        await asyncio.sleep(0.01)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert calls == [True]


@pytest.mark.asyncio
async def test_a_non_filled_event_does_not_trigger_the_pass():
    calls: list[bool] = []

    async def run_pass() -> None:
        calls.append(True)

    lock = asyncio.Lock()
    alerts = _FakeAlerts()

    async def order_events():
        order = _Order("1", "Live", [_Leg("SPXW  260711P07535000", "Buy to Close", 1)])
        yield {"type": "order_status", "order_id": "1", "status": "live", "raw": order}
        await _never()

    task = asyncio.create_task(consume_order_events(order_events, run_pass, lock, alerts))
    await asyncio.sleep(0.05)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert calls == []


@pytest.mark.asyncio
async def test_stream_death_reconnects_with_a_single_down_and_a_single_recovered_alert():
    calls: list[bool] = []

    async def run_pass() -> None:
        calls.append(True)

    lock = asyncio.Lock()
    alerts = _FakeAlerts()
    attempt = {"n": 0}
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def order_events():
        attempt["n"] += 1
        if attempt["n"] == 1:
            raise RuntimeError("stream died")
        yield _filled_event()
        await _never()

    task = asyncio.create_task(
        consume_order_events(order_events, run_pass, lock, alerts, sleep=fake_sleep))
    for _ in range(200):
        if calls:
            break
        await asyncio.sleep(0.01)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert calls == [True]                              # the retry's event still ran the pass
    assert sleeps == [1.0]                               # backoff used exactly once
    down = [c for c in alerts.calls if c[0] == "warning"]
    recovered = [c for c in alerts.calls if c[0] == "info"]
    assert len(down) == 1, "one alert when the stream goes down, not one per retry"
    assert len(recovered) == 1, "one alert when the stream recovers"


@pytest.mark.asyncio
async def test_repeated_failures_never_alert_more_than_once_while_still_down():
    calls: list[bool] = []

    async def run_pass() -> None:
        calls.append(True)

    lock = asyncio.Lock()
    alerts = _FakeAlerts()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 4:
            raise asyncio.CancelledError()   # stop the test's own loop deterministically

    async def order_events():
        raise RuntimeError("still down")
        yield {}  # pragma: no cover -- unreachable, keeps this an async generator

    task = asyncio.create_task(
        consume_order_events(order_events, run_pass, lock, alerts, sleep=fake_sleep))
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)

    assert calls == []
    down = [c for c in alerts.calls if c[0] == "warning"]
    assert len(down) == 1, "still-down retries must not re-alert every attempt"
    # capped exponential backoff: 1.0, 2.0, 4.0, ... never exceeding max_backoff_s
    assert sleeps[:3] == [1.0, 2.0, 4.0]


@pytest.mark.asyncio
async def test_shutdown_cancels_the_consumer_cleanly():
    async def run_pass() -> None:
        pass

    lock = asyncio.Lock()
    alerts = _FakeAlerts()
    started = asyncio.Event()

    async def order_events():
        started.set()
        await _never()
        yield {}  # pragma: no cover -- unreachable

    task = asyncio.create_task(consume_order_events(order_events, run_pass, lock, alerts))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.asyncio
async def test_tick_and_push_never_run_the_pass_concurrently():
    """Debounce: the SAME lock the push consumer uses (run_pass_locked) is
    what server.py's _probe_once tick funnels through too. Simulate both
    callers racing a slow pass and assert at most one is ever in flight."""
    lock = asyncio.Lock()
    active = 0
    max_active = 0
    entered = asyncio.Event()

    async def run_pass() -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        entered.set()
        await asyncio.sleep(0.1)
        active -= 1

    alerts = _FakeAlerts()

    async def order_events():
        yield _filled_event()
        await _never()

    push_task = asyncio.create_task(consume_order_events(order_events, run_pass, lock, alerts))
    await asyncio.wait_for(entered.wait(), timeout=1.0)   # the push side is now mid-pass, lock held

    # the "tick" caller races it via the exact same entrypoint _probe_once uses
    tick_task = asyncio.create_task(run_pass_locked(lock, run_pass))
    await tick_task   # queues behind the lock, then runs -- never concurrently

    push_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await push_task

    assert max_active == 1, "push and tick must never run the pass concurrently"


# --- run_pass_if_idle -- the fallback poll's skip-if-busy sibling helper -------
# ITEM 1 follow-up (operator ruling 2026-07-11): the stop-fill FALLBACK poll
# (server.py's dedicated loop, MEIC_STOP_FILL_POLL_S) must never queue behind
# `stop_fill_lock` -- if a push-triggered pass or a still-running LEX ladder
# already holds it, the fallback tick skips outright and the NEXT tick
# re-checks. This is the deliberate asymmetry against the push path's own
# `run_pass_locked` (which blocks/queues so a fill event is never dropped).


@pytest.mark.asyncio
async def test_run_pass_if_idle_runs_the_pass_when_the_lock_is_free():
    calls: list[bool] = []

    async def run_pass() -> None:
        calls.append(True)

    lock = asyncio.Lock()

    ran = await run_pass_if_idle(lock, run_pass)

    assert ran is True
    assert calls == [True]
    assert not lock.locked()   # released again after the pass completes


@pytest.mark.asyncio
async def test_run_pass_if_idle_skips_without_blocking_when_the_lock_is_held():
    calls: list[bool] = []

    async def run_pass() -> None:
        calls.append(True)

    lock = asyncio.Lock()
    await lock.acquire()   # simulate a push-triggered pass (or a still-running LEX ladder) in flight
    try:
        ran = await asyncio.wait_for(run_pass_if_idle(lock, run_pass), timeout=0.2)
    finally:
        lock.release()

    assert ran is False
    assert calls == [], "a held lock must be skipped outright, never queued on"


@pytest.mark.asyncio
async def test_poll_tick_skips_while_push_holds_the_lock_and_resumes_next_tick():
    """The real interaction the dedicated poll loop relies on: a slow
    push-triggered pass (run_pass_locked) holds the lock; a poll tick that
    lands mid-pass (run_pass_if_idle) must skip immediately rather than wait
    -- and once the push pass releases the lock, the NEXT poll tick runs
    normally. Simulates two loop ticks against the SAME shared lock."""
    lock = asyncio.Lock()
    push_calls: list[str] = []
    entered = asyncio.Event()

    async def slow_push_pass() -> None:
        push_calls.append("push")
        entered.set()
        await asyncio.sleep(0.1)

    push_task = asyncio.create_task(run_pass_locked(lock, slow_push_pass))
    await asyncio.wait_for(entered.wait(), timeout=1.0)   # push is now mid-pass, lock held

    poll_calls: list[str] = []

    async def poll_pass() -> None:
        poll_calls.append("poll")

    # "tick 1": lands while the push pass still holds the lock -> skipped
    first_ran = await asyncio.wait_for(run_pass_if_idle(lock, poll_pass), timeout=0.2)
    assert first_ran is False
    assert poll_calls == []

    await push_task   # let the push pass finish and release the lock

    # "tick 2": the next tick re-checks and now runs normally
    second_ran = await run_pass_if_idle(lock, poll_pass)
    assert second_ran is True
    assert poll_calls == ["poll"]
