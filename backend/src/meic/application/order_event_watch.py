"""Event-driven stop-fill reaction (operator ruling 2026-07-11): "the stop
being hit triggers the long sale immediately; only if that fails does the
periodic check force it." Before this, `TastytradeAdapter.order_events()`
(the account order-status stream, STP-04/ORD-05/LEX-01) was fully
contract-tested (`test_order_events_account_stream_receives_status`) but
NOTHING in live_app ever consumed it -- `detect_and_recover_stop_fills`
(stop_fill_watch.py) ran ONLY off the ~60s health tick (`_probe_once`). The
SEVENTH member of the "exists but unwired" class.

This module writes NO new streaming primitive and NO new decision logic: it
reuses the adapter's existing `order_events()` async generator, and a
terminal-filled event is only a WAKE-UP -- it re-runs the SAME idempotent
`detect_and_recover_stop_fills` pass (that pass re-reads journal + broker
truth on every call, so a spurious or duplicate wake-up is harmless). The
matching/decision logic stays in exactly one place: `stop_fill_watch.py`.

Two callers, two helpers, one lock (`app.state.stop_fill_lock` in
server.py), deliberately ASYMMETRIC (operator ruling 2026-07-11, ITEM 1's
follow-up): a fill event that lands mid-pass must still cause a re-run
afterward, never be dropped, so the PUSH path below (`consume_order_events`)
always funnels through `run_pass_locked`, which BLOCKS until the lock is
free. The FALLBACK poll (server.py's dedicated stop-fill poll loop,
`MEIC_STOP_FILL_POLL_S`) is not reacting to a specific event -- it is only
re-checking whether the push path already covered everything -- so it
funnels through `run_pass_if_idle`, which SKIPS the tick outright (never
queues) when the lock is already held by a push-triggered pass or a still-
running LEX ladder; the next tick re-checks. Either way, the pass itself is
journal-terminal-aware (a side already sold/closed on the durable event log
is never re-tried -- see e.g. `test_decay_closed_side_is_never_lex_driven`
in tests/application/test_stop_fill_watch.py), so the fallback only ever
steps in for work the push path has not already completed; a spurious extra
tick is harmless either way.

Lifecycle: `consume_order_events` never raises except on `CancelledError`
(clean shutdown, propagated so the supervising task actually stops) -- any
other failure (stream death, a broker session not yet connected, ...)
reconnects with capped exponential backoff, alerting ONCE when the stream
goes down and ONCE when it recovers (never one alert per retry attempt).
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Awaitable, Callable


def _is_terminal_filled(event: dict[str, Any]) -> bool:
    """Same convention `TastytradeAdapter.fills_since`/`_confirmed_qty` use
    elsewhere in this codebase for a filled SDK order status:
    `str(status).lower().endswith("filled")` -- covers the normalized
    `order_events()` event shape (`status` already lowercased/stripped of its
    enum prefix by the adapter)."""
    return str(event.get("status", "")).lower().endswith("filled")


async def run_pass_locked(lock: asyncio.Lock, run_pass: Callable[[], Awaitable[None]]) -> None:
    """Run `run_pass` single-flighted against `lock` -- BLOCKS until `lock` is
    free, then runs. This is the PUSH path's helper (`consume_order_events`
    below): a terminal-filled order event is a specific piece of work that
    must never be silently dropped, so if the lock is already held (the
    fallback poll or another push event mid-pass), this call queues and
    still runs once the lock frees up. Contrast `run_pass_if_idle` below,
    the fallback poll's sibling helper, which skips instead of queuing."""
    async with lock:
        await run_pass()


async def run_pass_if_idle(lock: asyncio.Lock, run_pass: Callable[[], Awaitable[None]]) -> bool:
    """Skip-if-busy sibling to `run_pass_locked` above, for server.py's
    dedicated stop-fill FALLBACK poll loop (`MEIC_STOP_FILL_POLL_S`,
    operator ruling 2026-07-11). If `lock` is already held -- a push-
    triggered pass or a still-running LEX ladder -- this tick is SKIPPED
    entirely: it does NOT queue behind the lock. That asymmetry against
    `run_pass_locked` is deliberate: someone is already handling it, and the
    next poll tick re-checks regardless, so there is nothing to gain (and
    tick-scheduling to lose) by blocking here. The `lock.locked()` check and
    the `async with lock` acquire below have no `await` between them, so
    under asyncio's single-threaded cooperative scheduling no other task can
    grab the lock in between -- this is race-free without needing a
    non-blocking primitive.

    Returns True if the pass actually ran this tick, False if it was
    skipped because the lock was held.
    """
    if lock.locked():
        return False
    async with lock:
        await run_pass()
    return True


async def consume_order_events(
    order_events: Callable[[], AsyncIterator[dict[str, Any]]],
    run_pass: Callable[[], Awaitable[None]],
    lock: asyncio.Lock,
    alerts: Any,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    initial_backoff_s: float = 1.0,
    max_backoff_s: float = 60.0,
) -> None:
    """Supervised forever-loop: consume `order_events()` and react to a
    terminal-filled event by running the (locked) stop-fill pass. Intended to
    run as a single long-lived background task (server.py creates ONE, at
    startup, alongside the health loop) -- never crashes the app."""
    backoff = initial_backoff_s
    down = False
    while True:
        try:
            async for event in order_events():
                if down:
                    # A degraded fallback (the health tick) covered the gap
                    # while this was down -- info, not critical, on recovery.
                    alerts.alert("info", "order-event stream recovered "
                                         "(EC-STP-06 push path back up)")
                    down = False
                backoff = initial_backoff_s
                if _is_terminal_filled(event):
                    await run_pass_locked(lock, run_pass)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- must never crash the app
            if not down:
                alerts.alert("warning",
                             f"order-event stream failed, reconnecting with backoff "
                             f"(EC-STP-06 push path down -- the ~60s health tick "
                             f"still covers stop fills as the fallback): {exc!r}")
                down = True
            await sleep(backoff)
            backoff = min(backoff * 2, max_backoff_s)
