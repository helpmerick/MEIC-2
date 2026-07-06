"""RecoverLong — the LEX long-sale ladder (LEX-01..09).

Triggered by a short-leg stop fill (LEX-01): the orphaned long is ALWAYS sold
(LEX-07). Ladder (LEX-03): limit sell at the long's mid, repriced down one
tick per lex_reprice_seconds from the CURRENT quote, up to lex_reprice_attempts;
never below max(current bid, intrinsic) (LEX-04). Ladder exhausted or quote
unusable (LEX-02) ⇒ marketable-limit fallback at the current bid (LEX-05),
never a raw market order.

The reprice cadence and fill-race reconciliation (LEX-08/09) are driven by the
process manager; this service owns the price sequence and order submission.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from meic.domain.events import LongSaleRepriced, LongSaleStarted, LongSold, SideClosed
from meic.domain.ladder import RepriceLadder, lex_floor
from meic.domain.ticks import TickTable


@dataclass(frozen=True)
class Quote:
    bid: Decimal
    ask: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class LexResult:
    outcome: str  # "SOLD" | "FALLBACK_WORKING"
    prices_tried: tuple[Decimal, ...]


class RecoverLong:
    def __init__(
        self,
        broker,
        events: list,
        ticks: TickTable,
        *,
        lex_reprice_attempts: int = 4,
        lex_max_spread_ticks: int = 10,
    ) -> None:
        self._broker = broker
        self._events = events
        self._ticks = ticks
        self._attempts = lex_reprice_attempts
        self._max_spread_ticks = lex_max_spread_ticks

    def _quote_usable(self, q: Quote) -> bool:
        """LEX-02: not crossed, spread within bound."""
        if q.bid > q.ask:
            return False
        tick = self._ticks.tick_for(q.bid)
        return (q.ask - q.bid) <= self._max_spread_ticks * tick

    async def recover(
        self,
        *,
        entry_id: str,
        side: str,
        long_symbol: str,
        quote: Quote,
        intrinsic: Decimal,
        qty: int = 1,
    ) -> LexResult:
        self._events.append(LongSaleStarted(entry_id=entry_id, side=side))
        floor = lex_floor(quote.bid, intrinsic)  # LEX-04

        if not self._quote_usable(quote):
            await self._fallback(entry_id, side, long_symbol, quote.bid, qty)
            return LexResult("FALLBACK_WORKING", ())

        ladder = RepriceLadder(start=quote.mid, ticks=self._ticks, attempts=self._attempts, floor=floor)
        tried: list[Decimal] = []
        order_id = None
        for rung in ladder.prices():
            tried.append(rung.price)
            intent = {"action": "sell_to_close", "type": "limit", "tif": "Day",
                      "symbol": long_symbol, "price": rung.price, "qty": qty,
                      "idempotency_key": f"lex:{entry_id}:{side}"}
            order_id = await self._broker.submit(intent) if order_id is None \
                else await self._broker.replace(order_id, intent)
            self._events.append(LongSaleRepriced(entry_id=entry_id, side=side, step=rung.attempt, price=rung.price))
            if await self._filled(order_id):
                self._events.append(LongSold(entry_id=entry_id, side=side, recovery=rung.price))
                self._events.append(SideClosed(entry_id=entry_id, side=side))
                return LexResult("SOLD", tuple(tried))

        # LEX-05 fallback: marketable limit at the (re-fetched) current bid
        await self._fallback(entry_id, side, long_symbol, quote.bid, qty)
        return LexResult("FALLBACK_WORKING", tuple(tried))

    async def _fallback(self, entry_id, side, long_symbol, bid, qty) -> None:
        await self._broker.submit({
            "action": "sell_to_close", "type": "marketable_limit", "tif": "Day",
            "symbol": long_symbol, "price": bid, "qty": qty,
            "idempotency_key": f"lex-fallback:{entry_id}:{side}"})

    async def _filled(self, order_id) -> bool:
        for f in await self._broker.fills_since(None):
            if f.get("order_id") == order_id and not f.get("partial"):
                return True
        return False
