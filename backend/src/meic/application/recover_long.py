"""RecoverLong — the LEX long-sale ladder (LEX-01..09).

Triggered by a short-leg stop fill (LEX-01): the orphaned long is ALWAYS sold
(LEX-07). Ladder (LEX-03): limit sell at the long's mid, repriced down one
tick per lex_reprice_seconds from the CURRENT quote, up to lex_reprice_attempts;
never below max(current bid, intrinsic) (LEX-04). Ladder exhausted or quote
unusable (LEX-02) ⇒ marketable-limit fallback at the current bid (LEX-05),
never a raw market order.

ORDER-ID JOURNALING (LEX-01, v1.62): every order this ladder creates — the
initial rung submit, every cancel/replace (each mints a NEW broker id), and
the LEX-05 fallback — appends `LexOrderPlaced` with the broker's own order id
AT PLACEMENT (ORD-09 philosophy; DecayBuybackPlaced precedent), so LEX orders
are auditable and INCLUDED in the EOD-03 day-end order sweep.

MECHANICS FIX (2026-07-10, incident #2's own class, now on the LEX side): this
ladder used to replace blindly, with no clock and no real wait between rungs,
and its own hand-rolled `_filled()` did raw `dict.get(...)` on a fill record —
the exact live-shape crash `execute_entry._fill_matches` exists to prevent. A
live LEX sell fill registers a beat after it happens at the broker; replacing
it mid-flight either duplicates the sell (margin_check_failed, mirroring the
2026-07-09 entry-ladder incident) or crashes the ladder outright. Fixed by
mirroring `ExecuteEntryAttempt._work_order` exactly: a real clock, a real wait
per rung (`_await_fill`, polled — the same "first check is immediate" shape,
so synchronous paper/fake fills return with no wait), and a re-confirm of
NOT-filled immediately before every replace. Pricing/floor semantics (LEX-03/
04/05) are UNCHANGED — only the mechanics around them.

The reprice cadence and fill-race reconciliation (LEX-08/09) are driven by the
process manager; this service owns the price sequence and order submission.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from meic.config.fee_model import FeeModel
from meic.domain.events import (
    LexOrderPlaced,
    LongSaleRepriced,
    LongSaleStarted,
    LongSold,
    SideClosed,
)
from meic.domain.fees import fee_for_leg
from meic.domain.ladder import RepriceLadder, lex_floor
from meic.domain.ticks import TickTable

# Reused, not copied (the "4th-normalizer trap" the 2026-07-09 health audit
# flagged): the paper SimulatedBroker yields dicts, the live TastytradeAdapter
# yields SDK order OBJECTS, and a hand-rolled `.get(...)` here would crash on
# the live shape exactly as execute_entry's did before this normalizer existed.
from .execute_entry import _fill_matches
from .order_intent import OrderIntent, OrderLeg, right_of


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
        clock,
        events: list,
        ticks: TickTable,
        *,
        lex_reprice_seconds: float = 15,
        lex_reprice_attempts: int = 4,
        lex_max_spread_ticks: int = 10,
        lex_fill_poll_seconds: float = 2.0,
        fee_model: FeeModel | None = None,  # PNL-01
    ) -> None:
        self._broker = broker
        self._clock = clock
        self._events = events
        self._ticks = ticks
        self._reprice_seconds = lex_reprice_seconds
        self._attempts = lex_reprice_attempts
        self._max_spread_ticks = lex_max_spread_ticks
        self._poll_seconds = lex_fill_poll_seconds
        self._fee_model = fee_model or FeeModel()

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
        quote_stale: bool = False,
        adopt_order_id: str | None = None,
        adopt_price: Decimal | None = None,
    ) -> LexResult:
        # RPT-07 long recovery (2026-07-11, operator ruling): stamp the long's
        # mark at ladder start -- the honest best-available mark-at-stop,
        # whether this recover() call was reached via the push-detected path
        # (~1s after the stop) or the fallback catch-up poll.
        self._events.append(LongSaleStarted(
            entry_id=entry_id, side=side,
            mark_bid=quote.bid, mark_ask=quote.ask, intrinsic=intrinsic,
            at=self._clock.now().isoformat(),  # ORD-11 (v1.67)
        ))
        floor = lex_floor(quote.bid, intrinsic)  # LEX-04

        # LEX-02: crossed/wide judged here; the AGE criterion (no older than
        # max_quote_age_ms) is judged upstream where the snapshot's timestamps
        # live (DAT-02) and arrives as `quote_stale=True` — a stale quote must
        # never price a LADDER, but its bid can still price the LEX-05
        # marketable fallback ("quotes were unusable at LEX-02 ⇒ marketable
        # limit at the current bid" — the freshest bid the system has).
        if quote_stale or not self._quote_usable(quote):
            await self._fallback(entry_id, side, long_symbol, quote.bid, qty)
            return LexResult("FALLBACK_WORKING", ())

        ladder = RepriceLadder(start=quote.mid, ticks=self._ticks, attempts=self._attempts, floor=floor)
        tried: list[Decimal] = []
        # EC-LEX-08 (v1.63) supersession: when a resting intrinsic-floor
        # order already exists for this side (`adopt_order_id`), seed the
        # working order to IT rather than starting fresh -- the first rung
        # below then takes the existing cancel/replace branch (pre-check
        # `_filled` -> `replace` -> re-check on exception), which IS the
        # LEX-08 raced-fill-guarded supersession of the floor.
        working_id = adopt_order_id
        working_price = adopt_price
        for rung in ladder.prices():
            tried.append(rung.price)
            intent = OrderIntent(
                order_type="limit", tif="Day", kind="lex", entry_id=entry_id,
                contracts=qty, price=rung.price, idempotency_key=f"lex:{entry_id}:{side}",
                legs=(OrderLeg(right=right_of(side), action="sell_to_close",
                               qty=qty, symbol=long_symbol),))
            if working_id is None:
                working_id = await self._broker.submit(intent)
            else:
                # ORD-02-style reprice guard, mirrored from execute_entry's fix
                # (2026-07-09 incident #2 class): a live fill registers a beat
                # after it happens; blindly replacing it cancels nothing and
                # submits a SECOND sell (margin_check_failed) — or crashes the
                # whole ladder when the broker rejects the replace outright.
                # Re-confirm not-filled immediately before every replace.
                if await self._filled(working_id):
                    return self._sold(entry_id, side, working_price, tried, qty)
                try:
                    working_id = await self._broker.replace(working_id, intent)
                except Exception:
                    # REPRICE-RACE SWEEP (2026-07-11): the pre-check above narrows
                    # the window but does not close it — a live fill can still
                    # land in the gap between that check and this replace() call.
                    # Re-confirm before propagating: if it turns out the sell
                    # filled, this was the race, not a genuine error. A real,
                    # unrelated replace failure still propagates unchanged.
                    if await self._filled(working_id):
                        return self._sold(entry_id, side, working_price, tried, qty)
                    raise
            # LEX-01 order-id journaling (v1.62): every LEX order journals its
            # broker order id AT PLACEMENT — the initial submit AND every
            # replace (a replace mints a NEW id), mirroring DecayBuybackPlaced
            # (v1.61). By the time this order's fill can appear anywhere, its
            # id is already on the log, and the EOD-03 sweep audits it.
            self._events.append(LexOrderPlaced(
                entry_id=entry_id, side=side, broker_order_id=str(working_id),
                price=rung.price, kind="ladder", at=self._clock.now().isoformat()))
            working_price = rung.price
            self._events.append(LongSaleRepriced(entry_id=entry_id, side=side, step=rung.attempt,
                                                 price=rung.price, at=self._clock.now().isoformat()))

            # Paper/fake fills are synchronous, so this returns on the FIRST poll
            # without waiting; a live fill needs a beat to register, so we POLL
            # across the reprice interval and stop the moment it fills — never
            # waiting the whole interval (that would delay a known fill) and
            # never repricing a filled order.
            if await self._await_fill(working_id, self._reprice_seconds):
                return self._sold(entry_id, side, working_price, tried, qty)

        # LEX-05 fallback: last guard — it may have filled right at the final
        # deadline, between the last poll and here.
        if working_id is not None and await self._filled(working_id):
            return self._sold(entry_id, side, working_price, tried, qty)

        await self._fallback(entry_id, side, long_symbol, quote.bid, qty)
        return LexResult("FALLBACK_WORKING", tuple(tried))

    def _sold(self, entry_id: str, side: str, price: Decimal, tried: list[Decimal],
             qty: int = 1) -> LexResult:
        # PNL-01: closing a long (sell-to-close) -- commission-free. Per-share
        # (see domain/fees.py) -- never scaled by `qty` here.
        fee = fee_for_leg(self._fee_model, role="long", opening=False)
        at = self._clock.now().isoformat()  # ORD-11 (v1.67)
        self._events.append(LongSold(entry_id=entry_id, side=side, recovery=price, fee=fee, at=at))
        self._events.append(SideClosed(entry_id=entry_id, side=side, at=at))
        return LexResult("SOLD", tuple(tried))

    async def rest_floor(self, *, entry_id: str, side: str, long_symbol: str,
                         intrinsic: Decimal, qty: int = 1) -> tuple[str, Decimal]:
        """EC-LEX-08 (v1.63): rest a limit sell at the LEX-04 floor with
        bid = 0 -- max(one minimum tick, intrinsic floored to tick) -- for a
        side whose long has NO bid at all but a DAT-02-fresh underlying mark
        makes the floor computable. Journals `LexOrderPlaced(kind="floor")`
        at placement, like every other LEX order (ORD-09 philosophy).

        Deliberately NO `LongSaleStarted` here: there is no bid/ask to stamp
        honestly (RPT-07's mark-at-stop) -- that stamp is appended if/when a
        real quote lets `recover()` supersede this floor, priced off an
        actual quote at that point."""
        min_tick = self._ticks.tick_for(Decimal("0"))
        floor_price = max(min_tick, self._ticks.floor(intrinsic))
        order_id = await self._broker.submit(OrderIntent(
            order_type="limit", tif="Day", kind="lex", entry_id=entry_id,
            contracts=qty, price=floor_price, idempotency_key=f"lex-floor:{entry_id}:{side}",
            legs=(OrderLeg(right=right_of(side), action="sell_to_close",
                           qty=qty, symbol=long_symbol),)))
        self._events.append(LexOrderPlaced(
            entry_id=entry_id, side=side, broker_order_id=str(order_id),
            price=floor_price, kind="floor", at=self._clock.now().isoformat()))
        return str(order_id), floor_price

    def record_floor_sold(self, entry_id: str, side: str, recovery: Decimal,
                         qty: int = 1) -> None:
        """EC-LEX-08: the SAME terminal appends `_sold` makes (LongSold +
        SideClosed), for a resting floor order discovered FILLED between
        ticks -- the floor order is never inside a `recover()` ladder call,
        so nothing else watches it; `stop_fill_watch._try_recover` resolves
        its fill directly and calls this to close out the side honestly."""
        # PNL-01: closing a long (sell-to-close) -- commission-free. Per-share
        # (see domain/fees.py) -- never scaled by `qty` here.
        fee = fee_for_leg(self._fee_model, role="long", opening=False)
        at = self._clock.now().isoformat()  # ORD-11 (v1.67)
        self._events.append(LongSold(entry_id=entry_id, side=side, recovery=recovery, fee=fee, at=at))
        self._events.append(SideClosed(entry_id=entry_id, side=side, at=at))

    async def _await_fill(self, working_id, seconds: float) -> bool:
        """Poll for `working_id`'s fill for up to `seconds`, returning True as
        soon as it fills — the same shape as ExecuteEntryAttempt's own
        `_await_fill`, so the ladder never reprices a filled order."""
        deadline = self._clock.now() + timedelta(seconds=seconds)
        while True:
            if await self._filled(working_id):
                return True
            if self._clock.now() >= deadline:
                return False
            nxt = min(deadline, self._clock.now() + timedelta(seconds=self._poll_seconds))
            await self._clock.wait_until(nxt)

    async def _fallback(self, entry_id, side, long_symbol, bid, qty) -> None:
        order_id = await self._broker.submit(OrderIntent(
            order_type="marketable_limit", tif="Day", kind="lex", entry_id=entry_id,
            contracts=qty, price=bid, idempotency_key=f"lex-fallback:{entry_id}:{side}",
            legs=(OrderLeg(right=right_of(side), action="sell_to_close",
                           qty=qty, symbol=long_symbol),)))
        # LEX-01 order-id journaling (v1.62): the LEX-05 fallback is a LEX
        # order like any rung — its broker id is journaled at placement too.
        self._events.append(LexOrderPlaced(
            entry_id=entry_id, side=side, broker_order_id=str(order_id),
            price=bid, kind="fallback", at=self._clock.now().isoformat()))

    async def _filled(self, order_id) -> bool:
        for f in await self._broker.fills_since(None):
            if _fill_matches(f, order_id):
                return True
        return False
