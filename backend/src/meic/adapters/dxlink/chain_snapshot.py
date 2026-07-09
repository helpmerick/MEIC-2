"""Live chain snapshot — turns the broker's chain + DXLink quotes into the pure
domain's ChainSide pair (DAT-01, STK-04/10).

Two phases, because the ATM band is unknowable without spot:
  1. subscribe the index symbol, take the first quote -> spot
  2. compute the ±band strikes, subscribe their put/call symbols, collect marks

A strike only gets a Mark if it has a VALID two-sided quote (bid > 0, ask >= bid).
Anything else is a hole, and STK-10 completeness decides whether the chain is
usable. Quotes are staleness-stamped: a snapshot older than `max_age_seconds`
sets `stale`, and the entry gate refuses to trade on it (DAT-02) — which is
exactly what keeps a closed/illiquid market from producing a "valid" selection.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from meic.domain.chain import ChainSide, Mark


@dataclass(frozen=True)
class ChainSnapshot:
    spot: Decimal
    expiration: date
    put_side: ChainSide
    call_side: ChainSide
    put_band: tuple[Decimal, ...]
    call_band: tuple[Decimal, ...]
    symbols: dict[Decimal, tuple[str, str]]  # strike -> (put_symbol, call_symbol)
    taken_at: datetime
    stale: bool = False


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
    band_points: Decimal,
) -> tuple[ChainSide, ChainSide, tuple[Decimal, ...], tuple[Decimal, ...]]:
    """Pure: assemble both ChainSides from strike symbols + collected quotes.
    Puts run DOWN from the money, calls UP (strikes_toward_otm ordering)."""
    put_strikes = tuple(sorted((k for k in strike_symbols if k <= spot), reverse=True))
    call_strikes = tuple(sorted(k for k in strike_symbols if k >= spot))

    put_band = tuple(k for k in put_strikes if spot - k <= band_points)
    call_band = tuple(k for k in call_strikes if k - spot <= band_points)

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


async def snapshot_chain(
    session,
    *,
    underlying: str = "SPXW",
    index_symbol: str = "SPX",
    band_points: Decimal = Decimal("120"),
    spot_timeout_s: float = 10.0,
    quote_timeout_s: float = 12.0,
    max_age_seconds: float = 5.0,
    now=None,
) -> ChainSnapshot:
    """Snapshot the 0DTE chain. Never places an order; read-only."""
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Quote
    from tastytrade.instruments import NestedOptionChain

    chains = await NestedOptionChain.get(session, underlying)
    if not chains:
        raise RuntimeError(f"no {underlying} chain available")
    chain = chains[0]
    today = date.today()
    expiration = next((e for e in sorted(chain.expirations, key=lambda x: x.expiration_date)
                       if e.expiration_date >= today), None)
    if expiration is None:
        raise RuntimeError(f"no live {underlying} expiration")

    async with DXLinkStreamer(session) as streamer:
        idx = await _first_quote(streamer, Quote, index_symbol, spot_timeout_s)
        spot = (Decimal(str(idx.bid_price)) + Decimal(str(idx.ask_price))) / 2

        # Two mappings per strike: STREAMER symbols to collect quotes by (DXLink),
        # and OCC symbols for the returned `.symbols` (what orders would name).
        strike_streamers: dict[Decimal, tuple[str, str]] = {}
        strike_occ: dict[Decimal, tuple[str, str]] = {}
        for s in expiration.strikes:
            k = Decimal(str(s.strike_price))
            if abs(k - spot) <= band_points:
                strike_streamers[k] = streamer_pair(s)
                strike_occ[k] = occ_pair(s)
        if not strike_streamers:
            raise RuntimeError(f"no strikes within +/-{band_points} of spot {spot}")

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

    taken_at = (now() if callable(now) else None) or datetime.now(timezone.utc)
    # build_sides matches quotes to strikes by the SUBSCRIPTION symbols (streamer).
    put_side, call_side, put_band, call_band = build_sides(
        spot=spot, strike_symbols=strike_streamers, quotes=quotes, band_points=band_points)

    return ChainSnapshot(
        spot=spot, expiration=expiration.expiration_date,
        put_side=put_side, call_side=call_side,
        put_band=put_band, call_band=call_band, symbols=strike_occ,
        taken_at=taken_at, stale=elapsed > max_age_seconds)
