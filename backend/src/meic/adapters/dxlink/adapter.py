"""DXLinkAdapter — the live MarketDataFeed (doc 05 §6), DAT-01/02/05.

Wraps the tastytrade DXLink streamer for quotes/spot and REST chains for
startup snapshots (DAT-01). Owns staleness stamping (DAT-02): every tick is
stamped on arrival with the Clock, so the domain never sees a stale quote
without knowing it. This is the seam the full NFR-04 QuoteHub (single-writer,
generation-guarded, demand-reconnect) builds on — that hardening is its own
slice; this adapter provides the stamped-quote surface behind the port.

Live streaming is exercised by contract tests (pytest -m contract); the
staleness/translation logic here is offline-unit-tested. The domain imports
only the MarketDataFeed port + StampedQuote.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, AsyncIterator

from meic.domain.staleness import StampedQuote


def stamp_quote(raw: Any, *, now: datetime) -> StampedQuote:
    """Translate a DXLink Quote event into a StampedQuote (DAT-02 stamp).
    Isolated + pure so it is unit-testable without a live stream."""
    return StampedQuote(
        symbol=str(getattr(raw, "event_symbol", getattr(raw, "symbol", ""))),
        bid=Decimal(str(getattr(raw, "bid_price", getattr(raw, "bid", 0)))),
        ask=Decimal(str(getattr(raw, "ask_price", getattr(raw, "ask", 0)))),
        stamped_at=now,
    )


class DXLinkAdapter:
    def __init__(self, session, clock) -> None:
        self._session = session
        self._clock = clock  # Clock port — stamps every tick (DAT-02)
        self._streamer = None

    async def chain(self, underlying: str, expiration: str) -> Any:
        """REST chain snapshot for startup/selection (DAT-01)."""
        from tastytrade.instruments import NestedOptionChain
        chains = await NestedOptionChain.get(self._session, underlying)
        return chains[0] if chains else None

    async def quotes(self, symbols: list[str]) -> AsyncIterator[StampedQuote]:
        """Streaming, staleness-stamped quotes (DAT-01/02)."""
        from tastytrade import DXLinkStreamer
        from tastytrade.dxfeed import Quote

        async with DXLinkStreamer(self._session) as streamer:
            await streamer.subscribe(Quote, symbols)
            async for q in streamer.listen(Quote):
                yield stamp_quote(q, now=self._clock.now())

    async def spot(self, index: str) -> AsyncIterator[StampedQuote]:
        """SPX index spot for intrinsic (DAT-05), same staleness rule."""
        async for q in self.quotes([index]):
            yield q
