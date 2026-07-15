"""DAT-04a (v1.69): `chain_snapshot.snapshot_chain`'s piggybacked dxfeed
Profile subscription -- the halt-signal PROVIDER half of the ninth finding's
fix (`meic.adapters.dxlink.trading_status.TradingStatusStore` is the pure
STORE half, tested in test_trading_status.py).

Offline: fakes `tastytrade.DXLinkStreamer` and `NestedOptionChain.get` so this
never needs a sandbox connection -- `snapshot_chain` itself has no other
offline coverage anywhere in this repo (only contract-tested), so this file
also incidentally pins the ONE new call-site's wiring against a real function
signature, not just a mock.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from meic.adapters.dxlink import chain_snapshot as cs_mod

NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


class _FakeStrike:
    def __init__(self, strike_price, put_streamer, call_streamer, put_occ, call_occ):
        self.strike_price = strike_price
        self.put_streamer_symbol = put_streamer
        self.call_streamer_symbol = call_streamer
        self.put = put_occ
        self.call = call_occ


class _FakeExpiration:
    def __init__(self, expiration_date, strikes):
        self.expiration_date = expiration_date
        self.strikes = strikes


class _FakeChain:
    def __init__(self, expirations):
        self.expirations = expirations


class _FakeStreamer:
    """Minimal fake of tastytrade.DXLinkStreamer -- only the subscribe()/
    listen() surface `snapshot_chain` actually calls. Events are queued
    per-class ahead of time; `listen()` pops them in order (a NEW generator
    per call, same shared underlying list -- exactly like the real
    per-event-class memory stream) and then idles forever, matching the
    real streamer's "never exits" contract."""

    def __init__(self, session):
        self.session = session
        self._queues: dict[object, list] = {}
        self.subscribed: list[tuple] = []

    def queue(self, event_class, events):
        self._queues.setdefault(event_class, []).extend(events)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def subscribe(self, event_class, symbols, refresh_interval=0.1):
        self.subscribed.append((event_class, list(symbols)))

    async def listen(self, event_class):
        q = self._queues.setdefault(event_class, [])
        while q:
            ev = q.pop(0)
            yield ev
            await asyncio.sleep(0)
        while True:
            await asyncio.sleep(3600)   # exhausted: hang, never StopIteration


def _install_fakes(monkeypatch, *, profile_events, quote_events):
    import tastytrade
    import tastytrade.instruments as instruments_mod
    from tastytrade.dxfeed import Profile, Quote

    strike = _FakeStrike(Decimal("6000"), ".SPXWP6000", ".SPXWC6000",
                         "SPXW  260715P06000000", "SPXW  260715C06000000")
    chain = _FakeChain([_FakeExpiration(date(2026, 7, 15), [strike])])

    async def _fake_get(session, underlying):
        return [chain]

    monkeypatch.setattr(instruments_mod.NestedOptionChain, "get", _fake_get)

    holder = {}

    def _ctor(session):
        s = _FakeStreamer(session)
        s.queue(Quote, list(quote_events))
        s.queue(Profile, list(profile_events))
        holder["instance"] = s
        return s

    monkeypatch.setattr(tastytrade, "DXLinkStreamer", _ctor)
    return holder, Profile, Quote


def _quotes():
    return [
        SimpleNamespace(event_symbol="SPX", bid_price=5999, ask_price=6001),
        SimpleNamespace(event_symbol=".SPXWP6000", bid_price=1.0, ask_price=1.2),
        SimpleNamespace(event_symbol=".SPXWC6000", bid_price=1.0, ask_price=1.2),
    ]


def test_snapshot_chain_records_trading_status_off_the_same_connection(monkeypatch):
    holder, Profile, _Quote = _install_fakes(
        monkeypatch, quote_events=_quotes(),
        profile_events=[SimpleNamespace(event_symbol="SPX", trading_status="HALTED")])

    recorded = []
    snap = asyncio.run(cs_mod.snapshot_chain(
        session=object(), now=lambda: NOW,
        on_trading_status=lambda status, at: recorded.append((status, at))))

    assert recorded == [("HALTED", NOW)]
    assert (Profile, ["SPX"]) in holder["instance"].subscribed
    assert snap.spot == Decimal("6000")   # the ordinary chain-snapshot behavior is unaffected


def test_snapshot_chain_never_subscribes_profile_without_a_sink(monkeypatch):
    holder, Profile, _Quote = _install_fakes(
        monkeypatch, quote_events=_quotes(),
        profile_events=[SimpleNamespace(event_symbol="SPX", trading_status="ACTIVE")])

    asyncio.run(cs_mod.snapshot_chain(session=object(), now=lambda: NOW))   # on_trading_status=None

    assert all(cls is not Profile for cls, _symbols in holder["instance"].subscribed), (
        "no on_trading_status sink => no new subscription -- backward compatible, "
        "never a surprise Profile subscribe for an existing caller")


def test_a_missing_trading_status_reading_never_fails_the_snapshot(monkeypatch):
    # No Profile event ever arrives -- the bounded wait times out. DAT-04a:
    # best-effort, never a reason to fail or delay the chain snapshot itself.
    holder, _Profile, _Quote = _install_fakes(monkeypatch, quote_events=_quotes(), profile_events=[])

    recorded = []
    snap = asyncio.run(cs_mod.snapshot_chain(
        session=object(), now=lambda: NOW, trading_status_timeout_s=0.05,
        on_trading_status=lambda status, at: recorded.append((status, at))))

    assert recorded == []          # never called -- no reading arrived
    assert snap.spot == Decimal("6000")   # the snapshot itself still succeeded
