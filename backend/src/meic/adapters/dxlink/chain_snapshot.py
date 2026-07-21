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
    # UND-01/UND-04 (v1.86, defense in depth): the PROFILE NAME ("SPX" | "RUT"
    # | ...) of the underlying this snapshot was taken FROM, stamped at the
    # source by `snapshot_chain` below. The selector
    # (composition/live_selection.py `_attempt`) refuses to select when this
    # does not match the row's own underlying (skip
    # `chain_snapshot_underlying_mismatch`) -- so even if the per-underlying
    # stream ROUTING ever drifts, a RUT row can never be priced off an SPX
    # chain. Defaulted "SPX" so every pre-v1.86 constructor stays valid.
    underlying: str = "SPX"
    # UND-04 (v1.86, FIX-5): WHICH dxfeed event the spot came from --
    # "quote_mid" (two-sided index Quote, SPX's normal case) or "trade_last"
    # (the index's Trade last price -- RUT's case: its index Quote publishes
    # NaN bid/ask, cert triage 2026-07-21). Display/debug truth, stamped by
    # `snapshot_chain` via `_index_spot`; defaulted so every pre-FIX-5
    # constructor stays valid.
    spot_source: str = "quote_mid"


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


# `_first_quote` (the Quote-ONLY index-spot reader) is REMOVED (FIX-5,
# 2026-07-21): it assumed every index publishes a two-sided dxfeed Quote --
# true for SPX, NOT for RUT (NaN bid/ask; the SDK parser skips the event, so
# the listener never yields and the snapshot times out spotless). Its sole
# caller now uses `_index_spot` below. Do not resurrect a Quote-only reader.


async def _index_spot(
    streamer,
    symbol: str,
    *,
    quote_cls,
    trade_cls,
    timeout_s: float,
    prefer: str = "quote",
    grace_s: float = 1.0,
) -> tuple[Decimal, str]:
    """UND-01/UND-04 (v1.86, FIX-5 -- cert triage 2026-07-21): resolve the
    INDEX spot from whichever dxfeed event the index actually publishes.

    THE EVIDENCE THIS EXISTS FOR: the SPXW cert probe passes while the RUTW
    one fails in the same session -- the "RUT" index's dxfeed Quote carries
    NaN bid/ask (the tastytrade SDK's pydantic parser REJECTS the event and
    skips it, so the Quote listener simply never yields), while SPX's index
    Quote is two-sided. FTSE Russell index dissemination differs from Cboe's
    SPX. So: subscribe BOTH Quote and Trade for the index; prefer the Quote
    MID whenever a two-sided quote arrives; else fall back to the Trade's
    last price. NEVER silently synthesize -- if neither yields a number
    within `timeout_s` (or both streams end without one), this raises
    `asyncio.TimeoutError` exactly like the old Quote-only path: the
    snapshot fails closed, DAT-02 unchanged.

    `prefer` is the profile's `spot_event_hint` (SPX "quote", RUT "trade")
    -- it only ORDERS the preference (how long to grace-wait for a Quote
    when a Trade already arrived); BOTH paths remain live for BOTH profiles
    (defense in depth, same philosophy as the selector's snapshot-underlying
    mismatch guard). Returns `(spot, source)` with source "quote_mid" |
    "trade_last" -- stamped onto the ChainSnapshot as display/debug truth.

    `quote_cls`/`trade_cls` are injected (the SDK's Quote/Trade in
    production) so this is offline-unit-testable with a fake streamer and no
    tastytrade import (tests/application/test_tc_und_01.py).
    """
    await streamer.subscribe(quote_cls, [symbol])
    await streamer.subscribe(trade_cls, [symbol])

    async def _quote_mid():
        async for q in streamer.listen(quote_cls):
            if q.event_symbol == symbol and q.bid_price and q.ask_price:
                return (Decimal(str(q.bid_price)) + Decimal(str(q.ask_price))) / 2

    async def _trade_last():
        async for t in streamer.listen(trade_cls):
            if t.event_symbol == symbol and getattr(t, "price", None):
                return Decimal(str(t.price))

    quote_task = asyncio.ensure_future(_quote_mid())
    trade_task = asyncio.ensure_future(_trade_last())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    try:
        waiting = {quote_task, trade_task}
        trade_result: Decimal | None = None
        while waiting:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            done, waiting = await asyncio.wait(
                waiting, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                break  # deadline hit with nothing new
            if quote_task in done and quote_task.result() is not None:
                return quote_task.result(), "quote_mid"   # Quote mid preferred whenever present
            if trade_task in done and trade_task.result() is not None:
                trade_result = trade_task.result()
                if prefer != "quote" or quote_task.done():
                    return trade_result, "trade_last"
                # The hint says this index normally quotes: give the Quote a
                # BOUNDED grace to arrive before settling for the trade --
                # never the full timeout (that would starve a trade-only
                # index), never zero (a racing SPX trade print must not
                # flap the source).
                grace = min(grace_s, max(0.0, deadline - loop.time()))
                g_done, _ = await asyncio.wait({quote_task}, timeout=grace)
                if quote_task in g_done and quote_task.result() is not None:
                    return quote_task.result(), "quote_mid"
                return trade_result, "trade_last"
            # a listener ended with nothing usable -- keep waiting on the other
        if trade_result is not None:
            return trade_result, "trade_last"
        # Fail closed, exactly the old Quote-only path's failure mode.
        raise asyncio.TimeoutError(
            f"no index spot for {symbol} within {timeout_s}s (no two-sided Quote, no Trade)")
    finally:
        for task in (quote_task, trade_task):
            task.cancel()
        # Let the cancellations settle and swallow the CancelledError (and any
        # late listener error) so neither surfaces as a spurious warning.
        await asyncio.gather(quote_task, trade_task, return_exceptions=True)


async def snapshot_chain(
    session,
    *,
    underlying: str = "SPXW",
    index_symbol: str = "SPX",
    spot_timeout_s: float = 10.0,
    quote_timeout_s: float = 12.0,
    max_age_seconds: float = 5.0,
    now=None,
) -> ChainSnapshot:
    """Snapshot the 0DTE chain. Never places an order; read-only.

    DAT-04a v1.80 (retired): this used to also accept `on_trading_status`/
    `trading_status_timeout_s` and piggyback a dxfeed Profile subscription
    onto this same connection to feed a halt-signal store. Live use proved
    the underlying's Profile `trading_status` reads UNDEFINED in real
    trading windows -- the field was unusable, and the dedicated halt input
    was retired per the ratified contingency (see spec DAT-04a v1.80; the
    module this fed, `meic.adapters.dxlink.trading_status`, is deleted).
    Halt protection is now carried entirely by the freshness gates
    (DAT-02/STK-04/STK-10). Never re-add a Profile piggyback here without a
    fresh spec ruling.
    """
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Quote, Trade
    from tastytrade.instruments import NestedOptionChain

    from meic.domain.underlying import profile_by_root

    # UND-01/UND-04 (v1.86): resolve the profile ONCE, up front -- it names
    # the snapshot's source-underlying stamp (defense in depth, see
    # `ChainSnapshot.underlying`) AND orders the spot-event preference
    # (`spot_event_hint`, FIX-5: RUT's index Quote is NaN-bid/ask on dxfeed,
    # its spot comes from the Trade event -- cert triage 2026-07-21). An
    # unknown root stamps the root itself and defaults the hint to "quote".
    profile = profile_by_root(underlying)
    source_underlying = profile.name if profile is not None else underlying
    spot_hint = profile.spot_event_hint if profile is not None else "quote"

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
        # FIX-5 (UND-04, cert triage 2026-07-21): Quote-mid preferred,
        # Trade-last fallback, profile hint ordering the preference; neither
        # within the timeout -> asyncio.TimeoutError, fail-closed as before.
        spot, spot_source = await _index_spot(
            streamer, index_symbol, quote_cls=Quote, trade_cls=Trade,
            timeout_s=spot_timeout_s, prefer=spot_hint)

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

    taken_at = instant  # same instant `today` was derived from above -- one clock read
    # build_sides matches quotes to strikes by the SUBSCRIPTION symbols (streamer).
    put_side, call_side, put_band, call_band = build_sides(
        spot=spot, strike_symbols=strike_streamers, quotes=quotes,
        subscribe_span_pts=SUBSCRIBE_SPAN_PTS)

    # UND-01/UND-04 (v1.86): the source-underlying stamp was resolved up
    # front (`source_underlying`, with the FIX-5 spot hint) -- the selector's
    # mismatch guard refuses a snapshot whose stamp disagrees with the row.
    return ChainSnapshot(
        spot=spot, expiration=expiration.expiration_date,
        put_side=put_side, call_side=call_side,
        put_band=put_band, call_band=call_band, symbols=strike_occ,
        taken_at=taken_at, stale=elapsed > max_age_seconds,
        # NFR-04: the streamer map this function already built for its OWN
        # subscription above -- now published rather than discarded, so the live
        # quote-stream loop can subscribe in the same (only valid) namespace.
        streamer_symbols=strike_streamers,
        underlying=source_underlying,   # UND-04 (v1.86): defense-in-depth stamp
        spot_source=spot_source)        # UND-04 (v1.86, FIX-5): quote_mid | trade_last
