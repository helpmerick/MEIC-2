"""Live chain snapshot — turns the broker's chain + DXLink quotes into the pure
domain's ChainSide pair (DAT-01, STK-04/10).

Two phases, because the subscription universe is unknowable without spot:
  1. subscribe the index symbol, take the first quote -> spot
  2. subscribe the strikes within SUBSCRIBE_SPAN_PTS of spot, collect marks

A strike only gets a Mark if it has a VALID two-sided quote (bid > 0, ask >= bid).
Anything else is a hole, and STK-10 decides whether the chain is usable.
Quotes are staleness-stamped: a snapshot older than `max_age_seconds` sets
`stale`, and the entry gate refuses to trade on it (DAT-02) — which is exactly
what keeps a closed/illiquid market from producing a "valid" selection.

v1.51 note: `chain_atm_band_pts` (the old fixed subscription/gate band) is
RETIRED. Which strikes to SUBSCRIBE to is purely an implementation detail —
never the STK-10 gate itself, which now inspects each entry's own
TRADE-RELATIVE reachable set (domain/chain.py: `reachable_strikes`). This
module's `put_band`/`call_band` fields are the strikes that were SUBSCRIBED
(diagnostics only — kept for the live P/L card and contract-test visibility;
STK-10 no longer reads them).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from meic.application.market_calendar import trading_day
from meic.domain.chain import ChainSide, Mark

# Subscription breadth only (never the STK-10 gate, which inspects the
# trade-relative reachable set — see the module docstring). 250 pts
# comfortably covers any reachable set for the doc-06 config ranges
# (wing_width up to 100 + max_long_shifts up to 10 shifts, well inside 250).
SUBSCRIBE_SPAN_PTS = Decimal("250")


@dataclass(frozen=True)
class ChainSnapshot:
    spot: Decimal
    expiration: date
    put_side: ChainSide
    call_side: ChainSide
    put_band: tuple[Decimal, ...]   # diagnostics: strikes SUBSCRIBED, not the STK-10 gate
    call_band: tuple[Decimal, ...]  # diagnostics: strikes SUBSCRIBED, not the STK-10 gate
    # strike -> (put_symbol, call_symbol) in OCC form -- what ORDERS name (`occ_pair`).
    # NOT subscribable on DXLink; see `streamer_symbols` below.
    symbols: dict[Decimal, tuple[str, str]]
    taken_at: datetime
    stale: bool = False
    # NFR-04 (2026-07-13): strike -> (put_symbol, call_symbol) in DXFEED STREAMER
    # form (`streamer_pair`) -- the ONLY namespace DXLink will accept on a
    # subscription. `snapshot_chain` already computed this map to collect its own
    # quotes and then THREW IT AWAY; the live quote-stream loop (server.py
    # `_open_leg_symbols`/`_streamer_symbol`) needs it to translate a journaled
    # OCC leg symbol into something subscribable. Without it, subscribing by the
    # broker's OCC symbol makes DXLink silently return NO quotes -- identical on
    # the wire to "no market data" (the exact trap `streamer_pair`'s docstring
    # and tests/application/test_live_selection.py already warn about).
    # Defaulted so every existing constructor (and any snapshot restored from an
    # older shape) stays valid: an empty map means "cannot translate" -> the
    # caller declines to subscribe and falls back to the snapshot marks.
    streamer_symbols: dict[Decimal, tuple[str, str]] = field(default_factory=dict)


def _valid_mark(bid, ask) -> Mark | None:
    """A usable two-sided quote. Zero/absent bid or crossed book is a hole."""
    if bid is None or ask is None:
        return None
    b, a = Decimal(str(bid)), Decimal(str(ask))
    if b <= 0 or a <= 0 or a < b:
        return None
    return Mark(bid=b, ask=a)


def build_sides(
    *,
    spot: Decimal,
    strike_symbols: dict[Decimal, tuple[str, str]],
    quotes: dict[str, tuple],           # symbol -> (bid, ask)
    subscribe_span_pts: Decimal = SUBSCRIBE_SPAN_PTS,
) -> tuple[ChainSide, ChainSide, tuple[Decimal, ...], tuple[Decimal, ...]]:
    """Pure: assemble both ChainSides from strike symbols + collected quotes.
    Puts run DOWN from the money, calls UP (strikes_toward_otm ordering).

    The returned `put_band`/`call_band` tuples are diagnostics (which strikes
    were within the subscription span) — NOT the STK-10 gate, which inspects
    the trade-relative reachable set (domain/chain.py: `reachable_strikes`)."""
    put_strikes = tuple(sorted((k for k in strike_symbols if k <= spot), reverse=True))
    call_strikes = tuple(sorted(k for k in strike_symbols if k >= spot))

    put_band = tuple(k for k in put_strikes if spot - k <= subscribe_span_pts)
    call_band = tuple(k for k in call_strikes if k - spot <= subscribe_span_pts)

    put_marks: dict[Decimal, Mark] = {}
    call_marks: dict[Decimal, Mark] = {}
    for strike, (put_sym, call_sym) in strike_symbols.items():
        if put_sym in quotes:
            m = _valid_mark(*quotes[put_sym])
            if m is not None:
                put_marks[strike] = m
        if call_sym in quotes:
            m = _valid_mark(*quotes[call_sym])
            if m is not None:
                call_marks[strike] = m

    return (ChainSide(put_strikes, put_marks), ChainSide(call_strikes, call_marks),
            put_band, call_band)


def streamer_pair(strike) -> tuple[str, str]:
    """The dxfeed STREAMER symbols for a strike's put and call — what DXLink
    quotes are keyed by (e.g. '.SPXW260709P7315'). NOT the OCC symbol ('SPXW
    260709P07315000'): DXLink silently ignores an OCC subscription and sends no
    quotes, which reads identically to 'no market data' — the bug this fixes."""
    return (strike.put_streamer_symbol, strike.call_streamer_symbol)


def occ_pair(strike) -> tuple[str, str]:
    """The OCC symbols — what ORDERS name (the ACL/broker speak OCC, not dxfeed)."""
    return (strike.put, strike.call)


async def _first_quote(streamer, quote_cls, symbol: str, timeout_s: float):
    await streamer.subscribe(quote_cls, [symbol])
    async def _wait():
        async for q in streamer.listen(quote_cls):
            if q.event_symbol == symbol and q.bid_price and q.ask_price:
                return q
    return await asyncio.wait_for(_wait(), timeout=timeout_s)


async def _first_trading_status(streamer, symbol: str, timeout_s: float) -> str | None:
    """DAT-04a: bounded, best-effort wait for the underlying's dxfeed Profile
    `trading_status`, piggybacked onto the SAME streamer session as the
    quotes above -- no new connection. Returns None (never raises) on a
    timeout or any hiccup; a missing reading this cycle is exactly what
    `trading_status.TradingStatusStore`'s own staleness bound (300 s) is for
    -- this helper must never fail or slow down the chain snapshot itself."""
    from tastytrade.dxfeed import Profile   # module-level fn -- snapshot_chain's own
                                             # import below is local to ITS frame only

    async def _wait():
        async for ev in streamer.listen(Profile):
            if getattr(ev, "event_symbol", symbol) == symbol:
                return ev.trading_status
    try:
        return await asyncio.wait_for(_wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return None


async def snapshot_chain(
    session,
    *,
    underlying: str = "SPXW",
    index_symbol: str = "SPX",
    spot_timeout_s: float = 10.0,
    quote_timeout_s: float = 12.0,
    max_age_seconds: float = 5.0,
    now=None,
    on_trading_status=None,           # DAT-04a: (status, at) sink -- see module docstring
    trading_status_timeout_s: float = 5.0,
) -> ChainSnapshot:
    """Snapshot the 0DTE chain. Never places an order; read-only.

    `on_trading_status`, when given, is called at most once with
    (dxfeed trading_status, instant) -- DAT-04a's halt-signal provider,
    piggybacked onto THIS SAME DXLink connection via a Profile subscription
    for `index_symbol` (no new connection, no new dependency). Best-effort:
    a slow or absent Profile reading never fails or delays this function
    beyond `trading_status_timeout_s` -- `TradingStatusStore`'s own
    staleness bound is what governs whether the last reading is still
    usable, not this call.
    """
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Profile, Quote
    from tastytrade.instruments import NestedOptionChain

    chains = await NestedOptionChain.get(session, underlying)
    if not chains:
        raise RuntimeError(f"no {underlying} chain available")
    chain = chains[0]
    # DAY-03: the current trading day is the ET one, never the OS/operator
    # machine's own local calendar date (`date.today()` used to read that --
    # the same bug class as the confirmed live "today" bug elsewhere in the
    # codebase, just for 0DTE expiration selection instead of an entry id).
    instant = (now() if callable(now) else None) or datetime.now(timezone.utc)
    today = trading_day(instant)
    expiration = next((e for e in sorted(chain.expirations, key=lambda x: x.expiration_date)
                       if e.expiration_date >= today), None)
    if expiration is None:
        raise RuntimeError(f"no live {underlying} expiration")

    async with DXLinkStreamer(session) as streamer:
        idx = await _first_quote(streamer, Quote, index_symbol, spot_timeout_s)
        spot = (Decimal(str(idx.bid_price)) + Decimal(str(idx.ask_price))) / 2

        status_task = None
        if on_trading_status is not None:
            # DAT-04a (v1.69): piggyback the halt-signal Profile subscription
            # onto THIS SAME DXLink connection -- started concurrently with
            # the strike-quote collection below so it adds no serial latency
            # to the chain snapshot in the (expected) common case where the
            # Profile reading arrives quickly.
            await streamer.subscribe(Profile, [index_symbol])
            status_task = asyncio.create_task(
                _first_trading_status(streamer, index_symbol, trading_status_timeout_s))

        try:
            # Two mappings per strike: STREAMER symbols to collect quotes by
            # (DXLink), and OCC symbols for the returned `.symbols` (what
            # orders would name).
            strike_streamers: dict[Decimal, tuple[str, str]] = {}
            strike_occ: dict[Decimal, tuple[str, str]] = {}
            for s in expiration.strikes:
                k = Decimal(str(s.strike_price))
                if abs(k - spot) <= SUBSCRIBE_SPAN_PTS:
                    strike_streamers[k] = streamer_pair(s)
                    strike_occ[k] = occ_pair(s)
            if not strike_streamers:
                raise RuntimeError(f"no strikes within +/-{SUBSCRIBE_SPAN_PTS} of spot {spot}")

            wanted = {sym for pair in strike_streamers.values() for sym in pair}
            await streamer.subscribe(Quote, sorted(wanted))

            quotes: dict[str, tuple] = {}
            started = asyncio.get_event_loop().time()

            async def _collect():
                async for q in streamer.listen(Quote):
                    if q.event_symbol in wanted:
                        quotes[q.event_symbol] = (q.bid_price, q.ask_price)
                    if len(quotes) >= len(wanted):
                        return

            try:
                await asyncio.wait_for(_collect(), timeout=quote_timeout_s)
            except asyncio.TimeoutError:
                pass  # partial book — STK-10 completeness decides usability

            elapsed = asyncio.get_event_loop().time() - started

            if status_task is not None:
                # DAT-04a: best-effort, never fails the snapshot -- an
                # unusable reading this cycle is exactly what the store's own
                # staleness bound is for, not a reason to raise here.
                try:
                    status = await status_task
                except Exception:  # noqa: BLE001 — the piggybacked read must never fail this call
                    status = None
                if status is not None:
                    on_trading_status(status, instant)
        finally:
            # DAT-04a: never leak the background Profile task -- if anything
            # above raised before the `await status_task` was reached (e.g.
            # the "no strikes" RuntimeError), cancel it rather than letting it
            # dangle past this function's return.
            if status_task is not None and not status_task.done():
                status_task.cancel()

    taken_at = instant  # same instant `today` was derived from above -- one clock read
    # build_sides matches quotes to strikes by the SUBSCRIPTION symbols (streamer).
    put_side, call_side, put_band, call_band = build_sides(
        spot=spot, strike_symbols=strike_streamers, quotes=quotes,
        subscribe_span_pts=SUBSCRIBE_SPAN_PTS)

    return ChainSnapshot(
        spot=spot, expiration=expiration.expiration_date,
        put_side=put_side, call_side=call_side,
        put_band=put_band, call_band=call_band, symbols=strike_occ,
        taken_at=taken_at, stale=elapsed > max_age_seconds,
        # NFR-04: the streamer map this function already built for its OWN
        # subscription above -- now published rather than discarded, so the live
        # quote-stream loop can subscribe in the same (only valid) namespace.
        streamer_symbols=strike_streamers)
