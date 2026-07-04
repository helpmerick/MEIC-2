"""FakeMarketData — scripted MarketDataFeed for the doc-04 harness.

Doc 04 harness requirements: stream staleness and scripted failures. Quotes
and index ticks are staleness-stamped (`stamped_at`, from the FakeClock) so
DAT-02 gates can be exercised; `go_silent()` freezes the streams (staleness
grows with the clock) and `fail_next_chain()` scripts chain-fetch failures
(timeouts, holey chains) for STK-07/10/11 and NFR-04 scenarios.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from .fake_clock import FakeClock


class FakeMarketData:
    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self._chains: dict[tuple[str, str], Any] = {}
        self._chain_failures: list[Exception] = []
        self._quote_queues: dict[str, list[asyncio.Queue[Any]]] = {}
        self._spot_queues: dict[str, list[asyncio.Queue[Any]]] = {}
        self._silent = False

    # ------------------------------------------------------------------ script
    def set_chain(self, underlying: str, expiration: str, snapshot: Any) -> None:
        self._chains[(underlying, expiration)] = snapshot

    def fail_next_chain(self, *excs: Exception) -> None:
        """Script exceptions raised by the next chain() calls, in order."""
        self._chain_failures.extend(excs)

    def push_quote(self, symbol: str, quote: dict[str, Any]) -> None:
        if self._silent:
            return
        stamped = {"stamped_at": self._clock.now(), **quote}
        for q in self._quote_queues.get(symbol, []):
            q.put_nowait(stamped)

    def push_spot(self, index: str, tick: dict[str, Any]) -> None:
        if self._silent:
            return
        stamped = {"stamped_at": self._clock.now(), **tick}
        for q in self._spot_queues.get(index, []):
            q.put_nowait(stamped)

    def go_silent(self) -> None:
        """Simulate stream staleness: connected, but nothing ticks."""
        self._silent = True

    def resume(self) -> None:
        self._silent = False

    # ------------------------------------------------------------ MarketDataFeed
    async def chain(self, underlying: str, expiration: str) -> Any:
        if self._chain_failures:
            raise self._chain_failures.pop(0)
        try:
            return self._chains[(underlying, expiration)]
        except KeyError:
            raise LookupError(f"no scripted chain for {underlying} {expiration}") from None

    def quotes(self, symbols: list[str]) -> AsyncIterator[Any]:
        queues = []
        for s in symbols:
            q: asyncio.Queue[Any] = asyncio.Queue()
            self._quote_queues.setdefault(s, []).append(q)
            queues.append(q)

        async def _stream() -> AsyncIterator[Any]:
            while True:
                gets = [asyncio.ensure_future(q.get()) for q in queues]
                done, pending = await asyncio.wait(gets, return_when=asyncio.FIRST_COMPLETED)
                for p in pending:
                    p.cancel()
                for d in done:
                    yield d.result()

        return _stream()

    def spot(self, index: str) -> AsyncIterator[Any]:
        q: asyncio.Queue[Any] = asyncio.Queue()
        self._spot_queues.setdefault(index, []).append(q)

        async def _stream() -> AsyncIterator[Any]:
            while True:
                yield await q.get()

        return _stream()
