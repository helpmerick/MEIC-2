"""NFR-04 (2026-07-13): the live quote-stream loop that keeps `QuoteHub`
ticking off the CURRENTLY OPEN entries' leg symbols (ORD-09) -- the seam
`_live_pnl_enricher`/`_entry_profit_pct_now` now prefer over the ~60s chain
snapshot (see tests/adapters/test_live_pnl_enricher.py and
test_exit_evaluator.py for the enricher/evaluator side of this). Unit-tested
against fakes (no DXLink, no broker), mirroring this package's existing style.
"""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from meic.adapters.api.server import (
    _open_leg_symbols,
    _run_quote_stream_loop,
    _stream_open_entry_quotes,
    _streamer_symbol,
)
from meic.adapters.dxlink.chain_snapshot import ChainSnapshot
from meic.domain.chain import ChainSide
from meic.domain.events import CondorFilled, FilledLeg
from meic.domain.quote_hub import QuoteHub
from meic.domain.staleness import StampedQuote

# The journaled/broker OCC symbols the legs carry (ORD-09) -- note the double
# space, exactly as the broker reports them. DXLink has NEVER heard of these.
PUT_SHORT_SYM = "SPXW  260713P07535000"
PUT_LONG_SYM = "SPXW  260713P07510000"
CALL_SHORT_SYM = "SPXW  260713C07540000"
CALL_LONG_SYM = "SPXW  260713C07575000"
E2_SYM = "SPXW  260713P07000000"

# The DXFEED STREAMER symbols for those same strikes -- what DXLink actually
# speaks, and therefore what the hub is keyed by.
PUT_SHORT_STREAMER = ".SPXW260713P7535"
PUT_LONG_STREAMER = ".SPXW260713P7510"
CALL_SHORT_STREAMER = ".SPXW260713C7540"
CALL_LONG_STREAMER = ".SPXW260713C7575"
E2_STREAMER = ".SPXW260713P7000"

STREAMER_MAP = {
    D("7535"): (PUT_SHORT_STREAMER, ".SPXW260713C7535"),
    D("7510"): (PUT_LONG_STREAMER, ".SPXW260713C7510"),
    D("7540"): (".SPXW260713P7540", CALL_SHORT_STREAMER),
    D("7575"): (".SPXW260713P7575", CALL_LONG_STREAMER),
    D("7000"): (E2_STREAMER, ".SPXW260713C7000"),
}

ALL_OCC = {PUT_SHORT_SYM, PUT_LONG_SYM, CALL_SHORT_SYM, CALL_LONG_SYM, E2_SYM}

NOW = datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)


def _snapshot(streamer_symbols=None) -> ChainSnapshot:
    return ChainSnapshot(
        spot=D("7550"), expiration=None,
        put_side=ChainSide(strikes_toward_otm=(), marks={}),
        call_side=ChainSide(strikes_toward_otm=(), marks={}),
        put_band=(), call_band=(), symbols={}, taken_at=NOW,
        streamer_symbols=STREAMER_MAP if streamer_symbols is None else streamer_symbols)


class _Snaps:
    def __init__(self, last=None):
        self.last = last


def _legs():
    return (
        FilledLeg(symbol=PUT_SHORT_SYM, right="P", role="short", qty=1, price=D("1.80")),
        FilledLeg(symbol=PUT_LONG_SYM, right="P", role="long", qty=1, price=D("0.08")),
        FilledLeg(symbol=CALL_SHORT_SYM, right="C", role="short", qty=1, price=D("1.95")),
        FilledLeg(symbol=CALL_LONG_SYM, right="C", role="long", qty=1, price=D("0.07")),
    )


class _Comp:
    def __init__(self, events=None):
        self.events = list(events or [])


class _Alerts:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def alert(self, level, message, **context):
        self.messages.append((level, message))


# --- _streamer_symbol: the OCC -> STREAMER translation ------------------------

def test_streamer_symbol_translates_occ_to_the_dxfeed_streamer_symbol():
    """The whole point: DXLink does not know the OCC namespace. Pin the exact
    translation for a realistic broker symbol (double space and all)."""
    snap = _snapshot()
    assert _streamer_symbol(snap, CALL_LONG_SYM, "CALL") == CALL_LONG_STREAMER
    assert _streamer_symbol(snap, PUT_SHORT_SYM, "PUT") == PUT_SHORT_STREAMER
    # ...and the result is NEVER the OCC string it came from
    assert _streamer_symbol(snap, CALL_LONG_SYM, "CALL") != CALL_LONG_SYM


def test_streamer_symbol_picks_the_right_side_of_the_pair():
    snap = _snapshot()
    # strike 7540 carries BOTH a put and a call streamer symbol; the leg's side decides.
    assert _streamer_symbol(snap, CALL_SHORT_SYM, "CALL") == CALL_SHORT_STREAMER
    assert _streamer_symbol(snap, "SPXW  260713P07540000", "PUT") == ".SPXW260713P7540"


def test_streamer_symbol_is_none_when_it_cannot_translate():
    """No snapshot / no map / strike outside the subscribed span -> None, so the
    caller declines to subscribe and falls back. NEVER a guessed symbol."""
    assert _streamer_symbol(None, CALL_LONG_SYM, "CALL") is None
    assert _streamer_symbol(_snapshot(streamer_symbols={}), CALL_LONG_SYM, "CALL") is None
    far_otm = "SPXW  260713C09999000"   # strike 9999 not in the map
    assert _streamer_symbol(_snapshot(), far_otm, "CALL") is None


# --- _open_leg_symbols: the SUBSCRIPTION universe ----------------------------

def test_open_leg_symbols_returns_streamer_symbols_never_occ():
    """THE REGRESSION THIS PINS: subscribing with OCC symbols makes DXLink
    silently send nothing (indistinguishable from 'no market data'), so the hub
    stays empty forever. The subscription set must be STREAMER symbols."""
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]

    symbols = _open_leg_symbols(events, _snapshot())

    assert symbols == {PUT_SHORT_STREAMER, PUT_LONG_STREAMER,
                       CALL_SHORT_STREAMER, CALL_LONG_STREAMER}
    assert not (symbols & ALL_OCC)             # no OCC string may ever leak in
    assert all(s.startswith(".") for s in symbols)   # dxfeed form


def test_open_leg_symbols_omits_legs_it_cannot_translate():
    """A leg whose strike is outside the subscribed span is simply not
    subscribed -- never guessed. The others still are."""
    partial = {k: v for k, v in STREAMER_MAP.items() if k != D("7575")}  # call long dropped

    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    symbols = _open_leg_symbols(events, _snapshot(streamer_symbols=partial))

    assert symbols == {PUT_SHORT_STREAMER, PUT_LONG_STREAMER, CALL_SHORT_STREAMER}
    assert CALL_LONG_STREAMER not in symbols


def test_open_leg_symbols_is_empty_with_no_snapshot_to_translate_through():
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    assert _open_leg_symbols(events, None) == set()


def test_open_leg_symbols_excludes_terminal_entries():
    from meic.domain.events import EntryClosed

    events = [
        CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs()),
        EntryClosed(entry_id="e1", initiator="manual"),
    ]
    assert _open_leg_symbols(events, _snapshot()) == set()


def test_open_leg_symbols_empty_with_no_entries():
    assert _open_leg_symbols([], _snapshot()) == set()


# --- _stream_open_entry_quotes ------------------------------------------------

class _NeverCalledFeed:
    async def quotes(self, symbols):
        raise AssertionError("feed.quotes() must not be called with nothing subscribable")
        yield  # pragma: no cover -- makes this an async generator


def test_stream_idles_and_returns_when_no_entries_are_open():
    comp = _Comp(events=[])
    hub = QuoteHub()

    asyncio.run(_stream_open_entry_quotes(comp, hub, _NeverCalledFeed(), _Snaps(_snapshot()),
                                          idle_seconds=0.01))
    # returned without ever touching the feed -- proven by _NeverCalledFeed raising otherwise


def test_stream_idles_when_there_is_no_snapshot_to_translate_through():
    """Open entries but no chain snapshot yet: nothing is subscribable (we will
    NOT guess a streamer symbol), so idle rather than subscribe by OCC."""
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    comp = _Comp(events=events)
    hub = QuoteHub()

    asyncio.run(_stream_open_entry_quotes(comp, hub, _NeverCalledFeed(), _Snaps(None),
                                          idle_seconds=0.01))


class _RecordingFeed:
    """Records the symbols it was subscribed with, then yields the given ticks;
    appends `new_entry_event` between them so the consumer's change-detection
    (checked after each applied tick) fires on the second."""

    def __init__(self, comp, ticks, new_entry_event):
        self._comp = comp
        self._ticks = ticks
        self._new_entry_event = new_entry_event
        self.subscribed_with = None

    async def quotes(self, symbols):
        self.subscribed_with = list(symbols)
        yield self._ticks[0]
        self._comp.events.append(self._new_entry_event)
        yield self._ticks[1]


def test_stream_subscribes_by_streamer_symbol_applies_ticks_and_re_subscribes_on_change():
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    comp = _Comp(events=events)
    hub = QuoteHub()
    # Ticks arrive keyed by STREAMER symbol -- that is what DXLink sends, and so
    # that is how the hub ends up keyed.
    tick0 = StampedQuote(PUT_SHORT_STREAMER, D("1.60"), D("1.70"), NOW)
    tick1 = StampedQuote(PUT_LONG_STREAMER, D("0.07"), D("0.09"), NOW)
    new_entry = CondorFilled(entry_id="e2", net_credit=D("1.00"), legs=(
        FilledLeg(symbol=E2_SYM, right="P", role="short", qty=1, price=D("0.50")),
    ))
    feed = _RecordingFeed(comp, (tick0, tick1), new_entry)

    # Must return normally (no exception) once it notices entry e2 opened.
    asyncio.run(_stream_open_entry_quotes(comp, hub, feed, _Snaps(_snapshot()), idle_seconds=0.01))

    # THE FIX: subscribed in the streamer namespace, never OCC.
    assert set(feed.subscribed_with) == {PUT_SHORT_STREAMER, PUT_LONG_STREAMER,
                                         CALL_SHORT_STREAMER, CALL_LONG_STREAMER}
    assert not (set(feed.subscribed_with) & ALL_OCC)
    assert hub.mark(PUT_SHORT_STREAMER) == tick0   # applied before the change was noticed
    assert hub.mark(PUT_LONG_STREAMER) == tick1    # applied on the tick the change was noticed
    assert any(isinstance(e, CondorFilled) and e.entry_id == "e2" for e in comp.events)


# --- _run_quote_stream_loop: never crashes, marks sick, backs off, retries --

class _AlwaysRaisingFeed:
    async def quotes(self, symbols):
        raise RuntimeError("dxlink socket died")
        yield  # pragma: no cover -- makes this an async generator


def test_quote_stream_loop_swallows_a_stream_failure_marks_hub_sick_and_retries():
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    comp = _Comp(events=events)
    hub = QuoteHub()
    hub.open_generation()   # healthy before the loop starts
    alerts = _Alerts()

    async def _drive():
        task = asyncio.create_task(_run_quote_stream_loop(
            comp, hub, _AlwaysRaisingFeed(), _Snaps(_snapshot()), alerts,
            idle_seconds=0.01, retry_seconds=0.01, connected=lambda: True))
        try:
            await asyncio.wait_for(task, timeout=0.1)
        except asyncio.TimeoutError:
            pass  # expected: the loop runs forever -- that's the point
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_drive())

    assert hub.healthy is False   # mark_sick() was called
    assert len(alerts.messages) >= 1
    assert all(level == "warning" for level, _ in alerts.messages)
    assert all("dxlink socket died" in msg or "quote stream failed" in msg for _, msg in alerts.messages)


def test_quote_stream_loop_idles_without_touching_the_feed_when_not_connected():
    comp = _Comp(events=[])
    hub = QuoteHub()
    alerts = _Alerts()

    async def _drive():
        task = asyncio.create_task(_run_quote_stream_loop(
            comp, hub, _NeverCalledFeed(), _Snaps(_snapshot()), alerts,
            idle_seconds=0.01, retry_seconds=0.01, connected=lambda: False))
        try:
            await asyncio.wait_for(task, timeout=0.05)
        except asyncio.TimeoutError:
            pass
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_drive())

    assert hub.healthy is True   # never touched -- nothing failed
    assert alerts.messages == []
