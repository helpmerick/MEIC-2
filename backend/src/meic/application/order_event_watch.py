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
`detect_and_recover_stop_fills` pass the health tick already runs (that pass
re-reads journal + broker truth on every call, so a spurious or duplicate
wake-up is harmless). The matching/decision logic stays in exactly one
place: `stop_fill_watch.py`.

Debounce: `run_pass` is always invoked through a shared `asyncio.Lock` so a
push-triggered pass and the health tick's own pass never run concurrently
(the pass assumes single-flight within a process -- see server.py's
`_probe_once`, whose own serialization came for free from being the sole
caller before this).

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
    """Run `run_pass` single-flighted against `lock` -- the ONE place both the
    push consumer below and server.py's `_probe_once` tick must funnel
    through, so the two callers can never execute the pass concurrently."""
    async with lock:
        await run_pass()


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
