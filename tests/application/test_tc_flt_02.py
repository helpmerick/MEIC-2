"""TC-FLT-02 (RSK-01a concurrency + rails): flatten entries run concurrently;
under injected 429s every flatten order is exit-priority and none is dropped
(EC-API-02); flatten orders are never blocked by the daily order cap (RSK-08).
"""
import asyncio
from decimal import Decimal as D

from meic.application.close_entry import CloseEntry, LiveLeg
from meic.application.flatten_all import FlattenAll, OpenEntry
from meic.application.rate_limit import (
    PriorityRateLimiter,
    RateLimited,
    is_exit_priority,
)
from meic.domain.events import EntryClosed
from meic.domain.risk import OrderCap


# --- (1) entries flatten concurrently ----------------------------------------

def test_tc_flt_02a_entries_flatten_concurrently():
    """The book is closed concurrently (asyncio.gather), so submits from
    different entries interleave — peak in-flight > 1."""
    active = 0
    peak = 0

    class ConcurrencyBroker:
        async def submit(self, order):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)  # hold the slot so others overlap
            active -= 1
            return "ok"

        async def cancel(self, id):
            return {"result": "cancelled"}

    broker = ConcurrencyBroker()
    events: list = []
    flat = FlattenAll(CloseEntry(broker, events))
    book = [
        OpenEntry(f"e{n}", [LiveLeg(f"P{n}", "PUT", "short", -1),
                            LiveLeg(f"C{n}", "CALL", "short", -1)], D("0.05"))
        for n in range(3)
    ]
    asyncio.run(flat.flatten(book))

    assert peak >= 2  # entries flattened concurrently, not one-at-a-time
    assert sum(isinstance(e, EntryClosed) for e in events) == 3


# --- (2) under 429s every flatten order is exit-priority, none dropped --------

def test_tc_flt_02b_flatten_orders_are_exit_priority_and_never_dropped():
    """A flatten close is classified exit-priority; under a burst of 429s the
    limiter retries it to completion (never dropped) and dispatches it ahead of
    any entry-side request."""
    assert is_exit_priority("manual_flatten") and is_exit_priority("buy_to_close")
    assert not is_exit_priority("entry")

    limiter = PriorityRateLimiter(max_entry_retries=2)

    def flaky(n_429s: int, label: str):
        calls = {"n": 0}
        async def fn():
            if calls["n"] < n_429s:
                calls["n"] += 1
                raise RateLimited(label)
            return label
        return fn

    # two flatten (exit) requests each hit three 429s; one entry hits endless 429s
    requests = [
        (False, flaky(99, "entry"), "entry"),        # entry: exhausts retries -> dropped
        (True, flaky(3, "flatten-e1"), "flatten-e1"),
        (True, flaky(3, "flatten-e2"), "flatten-e2"),
    ]
    results = asyncio.run(limiter.run(requests))

    # every exit-side flatten landed; the entry was dropped, never an exit
    assert limiter.dispatched == ["flatten-e1", "flatten-e2"]  # exits first, in order
    assert limiter.dropped == ["entry"]
    assert results[1] == "flatten-e1" and results[2] == "flatten-e2"
    assert results[0] is None


# --- (3) flatten orders are never blocked by the daily order cap (RSK-08) -----

def test_tc_flt_02c_flatten_never_blocked_by_daily_order_cap():
    cap = OrderCap(cap=5, buffer=1)
    while cap.allow(exit_priority=False):
        cap.record()                              # exhaust the entry budget
    assert cap.allow(exit_priority=False) is False
    assert cap.allow(exit_priority=True) is True  # a flatten close is never capped
