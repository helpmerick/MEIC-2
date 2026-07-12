"""Global client-side rate limiter — EC-API-02.

One limiter in front of all broker I/O, with priority classes: exit-side
requests (stops, LEX sells, flatten closes) always outrank entries and queries,
and are NEVER dropped — a 429 backs off and retries until the request lands.
Entry/query requests give up after a bounded number of retries (the entry is
skipped by its normal gates, EC-ENT-05). Dispatch is serialized (one call in
flight) to model the single global client budget, which also makes the
exit-before-entry ordering deterministic.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable


class RateLimited(Exception):
    """The broker returned HTTP 429 (EC-API-02)."""


def is_exit_priority(kind: str) -> bool:
    """Classify an order/request kind into its priority class. Exit-side =
    anything risk-reducing that must outrank entries (EC-API-02 / RSK-08)."""
    return kind in {
        "stop", "stop_replace", "lex", "lex_sell", "flatten", "manual_flatten",
        "close", "buy_to_close", "sell_to_close",
    }


class PriorityRateLimiter:
    def __init__(self, *, max_entry_retries: int = 8, backoff_base: float = 0.0) -> None:
        self._max_entry_retries = max_entry_retries
        self._backoff = backoff_base
        self.dispatched: list[str] = []   # labels, in the order they went out
        self.dropped: list[str] = []      # labels dropped (only ever entry-side)

    async def run(self, requests: list[tuple[bool, Callable[[], Awaitable], str]]) -> list:
        """Dispatch a batch. Each request is (exit_priority, coro_factory, label).
        Exit-side requests are dispatched first and in submission order; entries
        follow. Returns results positionally (None for a dropped entry)."""
        order = sorted(range(len(requests)), key=lambda i: (not requests[i][0], i))
        results: dict[int, object] = {}
        for i in order:
            exit_priority, fn, label = requests[i]
            results[i] = await self._dispatch(exit_priority, fn, label)
        return [results[i] for i in range(len(requests))]

    async def _dispatch(self, exit_priority: bool, fn: Callable[[], Awaitable], label: str):
        attempt = 0
        while True:
            try:
                result = await fn()
                self.dispatched.append(label)
                return result
            except RateLimited:
                attempt += 1
                # exit-side is never dropped: keep backing off and retrying
                if not exit_priority and attempt > self._max_entry_retries:
                    self.dropped.append(label)
                    return None
                if self._backoff:
                    await asyncio.sleep(self._backoff * attempt)
