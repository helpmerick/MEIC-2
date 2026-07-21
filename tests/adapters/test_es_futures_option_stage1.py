"""UND-04 (/ES Stage 1, 2026-07-21) — the futures-option ADAPTER PATH only.

/ES stays `enabled=False` this stage (validation still refuses it for live
entries -- the F3 safety layer + enable is Stage 2, UND-03/TC-UND-02). These
tests prove the PLUMBING Stage 2 will need already works, offline, and that
the SPX/RUT cash-index path is byte-identical past every dispatch seam this
stage adds:

  1. the futures-option chain-fetch branch (chain_snapshot.py) -- front-future
     resolution, expiration selection across subchains, and strike/streamer
     symbol harvesting off the SDK's `Strike` nodes (mirrors the FIX-5 fake-
     streamer style already used by tests/application/test_tc_und_01.py);
  2. the ACL order-build branch (adapter.py `_option_for`/`_build_order`) --
     a futures-option intent resolves via `FutureOption.get`, never
     `occ_symbol` (structurally equity-only);
  3. the fill-parse branch (adapter.py `fill_legs`) -- a futures-option leg's
     right is read off its OWN symbol layout (fut_symbol.py), never the
     equity OCC column-12 convention;
  4. `reporting/folds.py::imported_fill_dollars` resolves the multiplier
     PER-UNDERLYING (the FLAG left in the RUT phase) so a future imported
     /ES fill values at x50, not x100;
  5. every dispatch seam above takes the OLD, untouched code path for a
     cash-index (SPX/RUT) profile or a keyless default OrderIntent.
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import date, datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from meic.adapters.dxlink.chain_snapshot import (
    _futures_strike_symbols,
    _nearest_futures_expiration,
    _resolve_front_future,
    _resolve_front_streamer_symbol,
    snapshot_chain,
)
from meic.adapters.tastytrade.adapter import TastytradeAdapter
from meic.adapters.tastytrade.fut_symbol import parse_future_option_symbol
from meic.application.order_intent import OrderIntent, OrderLeg
from meic.domain.underlying import PROFILES

EXP = date(2026, 7, 21)
ES_FUT_SYM = "./ESU6 E3BN6 260721C7185"
# FIX 5 (PROD probe 2026-07-21): the front future's tastytrade symbol vs its
# DXFEED streamer symbol -- DELIBERATELY different, the crux of the bug.
ES_FRONT_SYM = "/ESU6"
ES_FRONT_STREAMER = "/ESU26:XCME"


def _jwt(iss: str) -> str:
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'EdDSA'})}.{seg({'iss': iss})}.sig"


CERT = _jwt("https://api.sandbox.tastyworks.com")


async def _resolved(v):
    return v


# --- profile shape (UND-01/02) ------------------------------------------------

def test_es_profile_carries_the_verified_facts_and_stays_disabled():
    profile = PROFILES["/ES"]
    assert profile.multiplier == D("50")
    assert profile.instrument_class == "futures_option"
    assert profile.option_root == "/ES"          # the chain-fetch key, PROD probe 2026-07-21
    # STK-08: genuinely per-profile ticks, not the shared cash-index table.
    assert profile.ticks.tick_for(D("3.00")) == D("0.05")
    assert profile.ticks.tick_for(D("10.00")) == D("0.10")
    assert profile.ticks.tick_for(D("50.00")) == D("0.25")
    assert profile.ticks.tick_for(D("500.00")) == D("0.50")
    from meic.domain.underlying import _INDEX_TICKS
    assert profile.tick_rungs != _INDEX_TICKS  # STK-08 v1.86 FLAG resolved
    # Stage 1: still NEVER tradeable this stage.
    assert profile.enabled is False
    assert "UND-03" in (profile.disabled_reason or "")


# --- 1. futures-option chain-fetch branch (chain_snapshot.py) -----------------

def _future(symbol, active_month):
    return SimpleNamespace(symbol=symbol, active_month=active_month)


def _strike(strike_price, put, call, put_streamer, call_streamer):
    return SimpleNamespace(strike_price=strike_price, put=put, call=call,
                           put_streamer_symbol=put_streamer, call_streamer_symbol=call_streamer)


def _expiration(exp_date, strikes):
    return SimpleNamespace(expiration_date=exp_date, strikes=strikes)


def _subchain(expirations):
    return SimpleNamespace(expirations=expirations)


def test_resolve_front_future_picks_the_active_month_future():
    chain = SimpleNamespace(futures=[
        _future("/ESU6", False), _future("/ESZ6", True), _future("/ESH7", False)])
    assert _resolve_front_future(chain).symbol == "/ESZ6"


def test_resolve_front_future_fails_closed_when_none_active():
    chain = SimpleNamespace(futures=[_future("/ESU6", False), _future("/ESH7", False)])
    with pytest.raises(RuntimeError, match="active-month"):
        _resolve_front_future(chain)


def test_resolve_front_future_fails_closed_when_more_than_one_active():
    """FIX 3 (fail-closed): >1 active_month=True is an ambiguous front month --
    raise rather than silently pick the first."""
    chain = SimpleNamespace(futures=[_future("/ESU6", True), _future("/ESZ6", True)])
    with pytest.raises(RuntimeError, match="exactly one active-month"):
        _resolve_front_future(chain)


def test_nearest_futures_expiration_scans_every_subchain_for_the_soonest_at_or_after_today():
    """UND-04: the daily option root VARIES by weekday (E3B/E4C/E4D/EW4) --
    the nearest live expiration must be found across ALL subchains, never
    just the first one."""
    today = date(2026, 7, 21)
    chain = SimpleNamespace(option_chains=[
        _subchain([_expiration(date(2026, 7, 20), [])]),    # already past -- excluded
        _subchain([_expiration(date(2026, 7, 23), []),
                  _expiration(date(2026, 7, 21), [])]),
    ])
    expiration = _nearest_futures_expiration(chain, today)
    assert expiration.expiration_date == date(2026, 7, 21)


def test_nearest_futures_expiration_is_none_when_nothing_is_live():
    today = date(2026, 7, 21)
    chain = SimpleNamespace(option_chains=[_subchain([_expiration(date(2026, 7, 20), [])])])
    assert _nearest_futures_expiration(chain, today) is None


def test_futures_strike_symbols_harvests_within_span_reusing_streamer_and_occ_pair():
    strikes = [
        _strike(D("6295"), "./ESU6 E3BN6 260721P6295", "./ESU6 E3BN6 260721C6295",
               ".E3BN26P6295:XCME", ".E3BN26C6295:XCME"),
        _strike(D("7000"), "./ESU6 E3BN6 260721P7000", "./ESU6 E3BN6 260721C7000",
               ".E3BN26P7000:XCME", ".E3BN26C7000:XCME"),   # far OTM, outside span
    ]
    expiration = _expiration(EXP, strikes)
    streamers, occs = _futures_strike_symbols(expiration, spot=D("6300"), span=D("250"))
    assert set(streamers) == {D("6295")}
    assert streamers[D("6295")] == (".E3BN26P6295:XCME", ".E3BN26C6295:XCME")
    assert occs[D("6295")] == ("./ESU6 E3BN6 260721P6295", "./ESU6 E3BN6 260721C6295")


# --- FIX 1: raw-JSON fetch with sparse-strike tolerance -----------------------
#
# The raw payload uses the SDK's own dasherized aliases (tastytrade
# `utils._dasherize`): `futures`/`option-chains`/`active-month`/`symbol`/
# `expiration-date`/`strikes`/`strike-price`/`call`/`put`/
# `call-streamer-symbol`/`put-streamer-symbol`.

def _raw_strike(strike_price, *, sparse=False):
    """A raw JSON strike dict. `sparse=True` omits the streamer symbols
    (the far-OTM shape that made the SDK's required-field model raise)."""
    d = {
        "strike-price": str(strike_price),
        "call": f"./ESU6 E3BN6 260721C{strike_price}",
        "put": f"./ESU6 E3BN6 260721P{strike_price}",
    }
    if not sparse:
        d["call-streamer-symbol"] = f".E3BN26C{strike_price}:XCME"
        d["put-streamer-symbol"] = f".E3BN26P{strike_price}:XCME"
    return d


def _raw_payload(futures, expirations):
    """A raw `/futures-option-chains/ES/nested` `data` payload (dasherized)."""
    return {
        "futures": futures,
        "option-chains": [{"expirations": expirations}],
    }


class _FakeStreamer:
    """subscribe/listen-shaped like DXLinkStreamer (mirrors the FIX-5 fakes).
    Quote yields `quotes`; Trade ends empty (front future quotes two-sided).
    `_subscribed`, when set to a list on the subclass, records every
    (event-class-name, symbols) subscription -- so a test can assert WHICH
    symbol the front-future spot was subscribed under (FIX 5)."""

    _quotes: tuple = ()
    _subscribed = None

    def __init__(self, session):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def subscribe(self, cls, symbols):
        if type(self)._subscribed is not None:
            type(self)._subscribed.append((cls.__name__, tuple(symbols)))

    async def listen(self, cls):
        from tastytrade.dxfeed import Quote
        if cls is Quote:
            for e in type(self)._quotes:
                yield e
        else:
            return
            yield  # pragma: no cover -- empty async generator for Trade


def _fake_session(payload, *, expect_url="/futures-option-chains/ES/nested"):
    """A session whose `_get` returns the raw payload (asserting the URL --
    the "/"-stripped root, exactly as the SDK builds it)."""
    class _S:
        async def _get(self, url):
            assert url == expect_url
            return payload
    return _S()


def _es_front(*, streamer=ES_FRONT_STREAMER):
    """The raw front-future JSON dict: tastytrade `symbol` PLUS the dasherized
    `streamer-symbol` the wire may carry (FIX 5 raw-key path)."""
    f = {"symbol": ES_FRONT_SYM, "active-month": True}
    if streamer is not None:
        f["streamer-symbol"] = streamer
    return f


def _front_quote():
    """The FRONT FUTURE's two-sided Quote, keyed by its STREAMER symbol (FIX 5:
    the future streams under `/ESU26:XCME`, not `/ESU6`)."""
    return SimpleNamespace(event_symbol=ES_FRONT_STREAMER,
                           bid_price=D("6299.75"), ask_price=D("6300.25"))


def _drive_snapshot(monkeypatch, payload, quotes, *, record=None):
    import tastytrade

    class _Streamer(_FakeStreamer):
        _quotes = tuple(quotes)
        _subscribed = record

    monkeypatch.setattr(tastytrade, "DXLinkStreamer", _Streamer)

    def _now():
        return datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc)

    return asyncio.run(snapshot_chain(
        session=_fake_session(payload), underlying="/ES", index_symbol="/ES", now=_now))


def test_snapshot_futures_option_chain_end_to_end_from_raw_json(monkeypatch):
    """The full async orchestration off the RAW JSON payload (FIX 1): front
    future resolved off `active-month`, nearest expiration picked, strikes
    harvested, spot resolved off the FRONT FUTURE's own Quote race -- never an
    index, never the SDK's whole-payload model. FIX 5: the spot streams under
    the future's DXFEED STREAMER symbol."""
    payload = _raw_payload(
        futures=[_es_front()],
        expirations=[{"expiration-date": "2026-07-21", "strikes": [_raw_strike("6300")]}])
    quotes = [
        _front_quote(),
        SimpleNamespace(event_symbol=".E3BN26P6300:XCME", bid_price=D("10.00"), ask_price=D("10.50")),
        SimpleNamespace(event_symbol=".E3BN26C6300:XCME", bid_price=D("11.00"), ask_price=D("11.50")),
    ]
    subs: list = []

    snap = _drive_snapshot(monkeypatch, payload, quotes, record=subs)

    assert snap.spot == D("6300.00")           # quote mid of 6299.75/6300.25
    assert snap.spot_source == "quote_mid"
    assert snap.underlying == "/ES"            # UND-04 defense-in-depth stamp
    assert snap.expiration == EXP
    assert snap.streamer_symbols[D("6300")] == (".E3BN26P6300:XCME", ".E3BN26C6300:XCME")
    assert snap.symbols[D("6300")] == ("./ESU6 E3BN6 260721P6300", "./ESU6 E3BN6 260721C6300")
    assert snap.put_side.marks[D("6300")].bid == D("10.00")
    assert snap.call_side.marks[D("6300")].ask == D("11.50")
    assert snap.stale is False
    # FIX 5: the spot subscription used the STREAMER symbol, never the bare
    # tastytrade symbol.
    assert ("Quote", (ES_FRONT_STREAMER,)) in subs
    assert not any(ES_FRONT_SYM in syms for _, syms in subs)


def test_snapshot_tolerates_sparse_far_strikes_and_harvests_the_complete_near_atm(monkeypatch):
    """FIX 1 -- THE gap the original offline fakes missed (real feed exposed
    it): a payload where some far-OTM strikes LACK streamer symbols must be
    TOLERATED -- the fetch skips them, harvests the complete near-ATM strikes,
    resolves the front future + spot, and does NOT raise (the SDK's
    whole-payload required-field model would have raised on the entire
    payload here)."""
    strikes = [
        _raw_strike("6300"),                 # near-ATM, complete
        _raw_strike("8500", sparse=True),    # far-OTM, missing streamer symbols
        _raw_strike("6305"),                 # near-ATM, complete
    ]
    payload = _raw_payload(
        futures=[_es_front()],
        expirations=[{"expiration-date": "2026-07-21", "strikes": strikes}])
    quotes = [
        _front_quote(),
        SimpleNamespace(event_symbol=".E3BN26P6300:XCME", bid_price=D("10.00"), ask_price=D("10.50")),
        SimpleNamespace(event_symbol=".E3BN26C6300:XCME", bid_price=D("11.00"), ask_price=D("11.50")),
    ]

    snap = _drive_snapshot(monkeypatch, payload, quotes)

    # the two complete near-ATM strikes are present; the sparse far strike is
    # silently absent -- never raised on.
    assert set(snap.streamer_symbols) == {D("6300"), D("6305")}
    assert D("8500") not in snap.streamer_symbols
    assert snap.spot == D("6300.00")
    assert snap.underlying == "/ES"


def test_snapshot_fails_closed_when_every_near_atm_strike_is_sparse(monkeypatch):
    """FIX 1: if, after skipping sparse strikes, NO usable strike remains near
    spot, the snapshot fails closed exactly as before (RuntimeError) -- a
    fully-sparse chain is untradeable, never a silent empty snapshot."""
    payload = _raw_payload(
        futures=[_es_front()],
        expirations=[{"expiration-date": "2026-07-21",
                      "strikes": [_raw_strike("6300", sparse=True)]}])
    quotes = [_front_quote()]

    with pytest.raises(RuntimeError, match="no strikes within"):
        _drive_snapshot(monkeypatch, payload, quotes)


# --- FIX 5: front-future DXFEED streamer-symbol resolution --------------------

def test_resolve_front_streamer_uses_the_raw_json_streamer_symbol_when_present():
    """FIX 5 (cheap path): when the raw future dict carries a non-empty
    `streamer-symbol`, use it -- no Future.get, no network. The streamer
    symbol (`/ESU26:XCME`) DIFFERS from the tastytrade symbol (`/ESU6`)."""
    front = SimpleNamespace(symbol=ES_FRONT_SYM, streamer_symbol=ES_FRONT_STREAMER)
    got = asyncio.run(_resolve_front_streamer_symbol(session=object(), front=front))
    assert got == ES_FRONT_STREAMER
    assert got != ES_FRONT_SYM


def test_resolve_front_streamer_falls_back_to_future_get_when_raw_key_absent(monkeypatch):
    """FIX 5 (fallback path): with no raw `streamer-symbol`, fetch the Future
    instrument once and read `.streamer_symbol` off it -- passing the front's
    tastytrade symbol to Future.get."""
    from tastytrade.instruments import Future

    calls: list = []

    async def _fake_get(session, symbol):
        calls.append(symbol)
        return SimpleNamespace(streamer_symbol=ES_FRONT_STREAMER, notional_multiplier=D("50"))
    monkeypatch.setattr(Future, "get", _fake_get)

    front = SimpleNamespace(symbol=ES_FRONT_SYM, streamer_symbol="")   # raw key empty
    got = asyncio.run(_resolve_front_streamer_symbol(session=object(), front=front))

    assert got == ES_FRONT_STREAMER
    assert calls == [ES_FRONT_SYM]   # Future.get called with the tastytrade symbol


def test_resolve_front_streamer_fails_closed_when_no_streamer_symbol_anywhere(monkeypatch):
    """FIX 5 fail-closed: neither the raw key NOR Future.get yields a non-empty
    streamer symbol -> raise (no spot -> snapshot fails). NEVER falls back to
    subscribing the tastytrade symbol."""
    from tastytrade.instruments import Future

    async def _fake_get(session, symbol):
        return SimpleNamespace(streamer_symbol="", notional_multiplier=D("50"))
    monkeypatch.setattr(Future, "get", _fake_get)

    front = SimpleNamespace(symbol=ES_FRONT_SYM, streamer_symbol="")
    with pytest.raises(RuntimeError, match="no DXFEED streamer symbol"):
        asyncio.run(_resolve_front_streamer_symbol(session=object(), front=front))


def test_snapshot_fails_closed_when_front_future_has_no_streamer_symbol(monkeypatch):
    """FIX 5 end-to-end fail-closed: a raw payload whose front future carries
    NO streamer symbol, and whose Future.get also reports none, makes the whole
    snapshot raise -- the spot can never be subscribed."""
    from tastytrade.instruments import Future

    async def _fake_get(session, symbol):
        return SimpleNamespace(streamer_symbol="", notional_multiplier=D("50"))
    monkeypatch.setattr(Future, "get", _fake_get)

    payload = _raw_payload(
        futures=[_es_front(streamer=None)],   # no raw streamer-symbol key
        expirations=[{"expiration-date": "2026-07-21", "strikes": [_raw_strike("6300")]}])

    with pytest.raises(RuntimeError, match="no DXFEED streamer symbol"):
        _drive_snapshot(monkeypatch, payload, quotes=[_front_quote()])


def test_snapshot_chain_dispatch_leaves_the_equity_path_untouched_for_a_cash_index_profile(
        monkeypatch):
    """Byte-identity seam: for SPX/RUT (`instrument_class="cash_index"`), the
    NEW branch added ahead of the equity code must never run -- proven by
    making the futures branch explode if reached, and the (pre-existing)
    equity fetch raise a distinguishable sentinel so we know the OLD path
    was the one actually exercised."""
    import meic.adapters.dxlink.chain_snapshot as cs_mod
    from tastytrade.instruments import NestedOptionChain

    def _boom(*a, **kw):
        raise AssertionError("the futures-option branch must not run for a cash-index profile")
    monkeypatch.setattr(cs_mod, "_snapshot_futures_option_chain", _boom)

    async def _equity_sentinel(session, symbol):
        raise RuntimeError("equity path reached, as expected")
    monkeypatch.setattr(NestedOptionChain, "get", _equity_sentinel)

    with pytest.raises(RuntimeError, match="equity path reached"):
        asyncio.run(cs_mod.snapshot_chain(session=object(), underlying="SPXW", index_symbol="SPX"))


# --- 2. ACL order-build branch (adapter.py) -----------------------------------

class _FakeOption:
    """Equity Option double -- unchanged from tests/adapters/test_occ_and_acl.py."""

    def __init__(self, symbol):
        self.symbol = symbol

    def build_leg(self, qty, action):
        from tastytrade.instruments import InstrumentType
        from tastytrade.order import Leg
        return Leg(instrument_type=InstrumentType.EQUITY_OPTION, symbol=self.symbol,
                  quantity=qty, action=action)


class _FakeFutureOption:
    def __init__(self, symbol):
        self.symbol = symbol

    def build_leg(self, qty, action):
        from tastytrade.instruments import InstrumentType
        from tastytrade.order import Leg
        return Leg(instrument_type=InstrumentType.FUTURE_OPTION, symbol=self.symbol,
                  quantity=qty, action=action)


def _strict_equity_adapter():
    """`_option_for` accepts ONLY a bare `symbol` -- no `instrument_class`
    kwarg at all -- so calling it any other way is a TypeError. Proves the
    equity call SHAPE (not just behaviour) is untouched by this stage."""
    a = TastytradeAdapter("secret", CERT, is_test=True)

    def _option_for(symbol):
        return _resolved(_FakeOption(symbol))
    a._option_for = _option_for
    return a


def _es_adapter():
    calls: list = []
    a = TastytradeAdapter("secret", CERT, is_test=True)

    async def _option_for(symbol, *, instrument_class="cash_index"):
        calls.append((symbol, instrument_class))
        return _FakeFutureOption(symbol)
    a._option_for = _option_for
    a._calls = calls
    return a


def test_acl_equity_intent_calls_option_for_with_the_old_bare_symbol_shape():
    """Byte-identity: a default (`instrument_class="cash_index"`) intent's
    `_build_order` must call `_option_for(symbol)` with NO kwarg -- the
    exact pre-Stage-1 call shape."""
    from meic.application.order_intent import condor_legs

    intent = OrderIntent(
        order_type="limit", tif="Day", contracts=1, kind="iron_condor",
        underlying="SPXW", expiration=EXP, price=D("4.00"), entry_id="d#1",
        legs=condor_legs(put_short=D("5990"), put_long=D("5940"),
                         call_short=D("6060"), call_long=D("6110"), contracts=1))

    order = asyncio.run(_strict_equity_adapter()._build_order(intent))
    assert len(order.legs) == 4   # never raised -- the bare-symbol call shape held


def test_acl_builds_a_future_option_leg_for_an_es_instrument_class_intent():
    """UND-04: a `instrument_class="futures_option"` intent resolves via
    `FutureOption.get` (injected here as `_es_adapter`'s fake), producing a
    FUTURE_OPTION leg carrying the broker's OWN futures-option symbol
    verbatim -- never built via `occ_symbol`."""
    a = _es_adapter()
    intent = OrderIntent(
        order_type="limit", tif="Day", contracts=1, price=D("12.50"),
        kind="iron_condor", underlying="/ES", instrument_class="futures_option",
        legs=(OrderLeg(right="C", action="sell_to_open", qty=1, symbol=ES_FUT_SYM),))

    order = asyncio.run(a._build_order(intent))

    from tastytrade.instruments import InstrumentType
    assert order.legs[0].symbol == ES_FUT_SYM
    assert order.legs[0].instrument_type == InstrumentType.FUTURE_OPTION
    assert a._calls == [(ES_FUT_SYM, "futures_option")]


def test_acl_refuses_a_futures_option_leg_with_no_resolved_symbol():
    """UND-04 fail-closed guard: `occ_symbol` is structurally equity-only
    (21-char, x1000-scaled OCC) and cannot build a futures-option symbol --
    a strike-only futures-option leg must raise, never silently build a
    wrong/garbage symbol via the equity constructor."""
    intent = OrderIntent(
        order_type="limit", tif="Day", contracts=1, price=D("12.50"),
        underlying="/ES", expiration=EXP, instrument_class="futures_option",
        legs=(OrderLeg(right="C", action="buy_to_close", qty=1, strike=D("7185")),))

    with pytest.raises(ValueError, match="futures-option leg requires a resolved symbol"):
        asyncio.run(_es_adapter()._build_order(intent))


def test_acl_refuses_a_futures_option_leg_with_an_empty_string_symbol():
    """FIX 4 (fail-closed): an EMPTY-STRING futures symbol is falsy, so it must
    also fail closed here rather than fall through to `occ_symbol` -- matching
    the equity path's `leg.symbol or occ_symbol(...)` truthiness."""
    intent = OrderIntent(
        order_type="limit", tif="Day", contracts=1, price=D("12.50"),
        underlying="/ES", instrument_class="futures_option",
        legs=(OrderLeg(right="C", action="buy_to_close", qty=1, symbol=""),))

    with pytest.raises(ValueError, match="futures-option leg requires a resolved symbol"):
        asyncio.run(_es_adapter()._build_order(intent))


# --- 3. fill-parse branch (adapter.py fill_legs) ------------------------------

def _adapter_with_live_orders(order):
    a = TastytradeAdapter("secret", CERT, is_test=True)
    a._account = SimpleNamespace(get_live_orders=lambda session: _resolved([order]))
    return a


def test_fill_legs_parses_the_futures_option_symbol_layout_via_instrument_type():
    """UND-04: the BROKER's own `instrument_type` on the leg (never anything
    on our side) decides the parse -- Future Option reads P/C off the
    futures-option symbol layout, never the equity OCC column-12 slice."""
    leg = SimpleNamespace(
        instrument_type="Future Option", symbol=ES_FUT_SYM, action="Sell to Open",
        quantity=D("1"), fills=[SimpleNamespace(fill_price=D("12.50"), quantity=D("1"))])
    order = SimpleNamespace(id="999", legs=[leg], status="Filled")

    legs = asyncio.run(_adapter_with_live_orders(order).fill_legs("999"))

    assert len(legs) == 1
    assert legs[0].symbol == ES_FUT_SYM        # verbatim, never reconstructed
    assert legs[0].right == "C"
    assert legs[0].role == "short"
    assert legs[0].qty == 1
    assert legs[0].price == D("12.50")


def test_fill_legs_equity_path_is_untouched_by_instrument_type_dispatch():
    """Byte-identity: an ordinary equity-option leg (no `instrument_type`
    attribute at all, matching every pre-Stage-1 test double) still parses
    via the OCC column-12 convention."""
    leg = SimpleNamespace(
        symbol="SPXW  260721P05990000", action="Sell to Open",
        quantity=D("2"), fills=None)
    order = SimpleNamespace(id="1", legs=[leg], status="Filled")

    legs = asyncio.run(_adapter_with_live_orders(order).fill_legs("1"))

    assert len(legs) == 1
    assert legs[0].right == "P"
    assert legs[0].role == "short"
    assert legs[0].symbol == "SPXW  260721P05990000"


def test_fill_legs_futures_put_with_no_instrument_type_is_never_misread_as_a_call():
    """FIX 2 (fail-closed) -- THE money-direction bug: a futures-option PUT
    fill with MISSING instrument_type. Column 12 of "./ESU6 E3BN6 260721P7185"
    is a SPACE, so the old equity slice returned "C" (a PUT read as a CALL).
    Shape-detection off the "./" prefix parses it correctly as a PUT."""
    put_sym = "./ESU6 E3BN6 260721P7185"
    assert put_sym[12:13] == " "   # the trap: the equity slice would say "C" here
    leg = SimpleNamespace(   # NO instrument_type attribute at all
        symbol=put_sym, action="Sell to Open",
        quantity=D("1"), fills=None)
    order = SimpleNamespace(id="7", legs=[leg], status="Filled")

    legs = asyncio.run(_adapter_with_live_orders(order).fill_legs("7"))

    assert legs[0].right == "P", "a futures PUT must never be misread as a CALL"


def test_fill_legs_fails_closed_on_an_ambiguous_leg_symbol_with_no_instrument_type():
    """FIX 2: an unrecognised symbol shape with no instrument_type RAISES
    (fail closed) rather than defaulting to the equity column slice, which
    could silently misread the right."""
    leg = SimpleNamespace(symbol="WEIRD", action="Sell to Open", quantity=D("1"), fills=None)
    order = SimpleNamespace(id="8", legs=[leg], status="Filled")

    with pytest.raises(ValueError, match="cannot determine option right"):
        asyncio.run(_adapter_with_live_orders(order).fill_legs("8"))


def test_fill_legs_equity_option_with_explicit_instrument_type_stays_on_the_occ_path():
    """FIX 2 byte-identity: an equity leg carrying its real
    instrument_type="Equity Option" still reads the right off the OCC
    column-12 slice, exactly as before."""
    leg = SimpleNamespace(
        instrument_type="Equity Option", symbol="SPXW  260721C06060000",
        action="Buy to Open", quantity=D("1"), fills=None)
    order = SimpleNamespace(id="9", legs=[leg], status="Filled")

    legs = asyncio.run(_adapter_with_live_orders(order).fill_legs("9"))
    assert legs[0].right == "C" and legs[0].role == "long"


def test_parse_future_option_symbol_reads_expiration_right_and_plain_strike():
    yymmdd, right, strike = parse_future_option_symbol(ES_FUT_SYM)
    assert (yymmdd, right, strike) == ("260721", "C", D("7185"))


def test_parse_future_option_symbol_rejects_an_unparseable_shape():
    with pytest.raises(ValueError, match="cannot parse"):
        parse_future_option_symbol("not a futures-option symbol")


# --- 4. imported_fill_dollars resolves the multiplier per-underlying ---------

def _imported(*, underlying="SPX", price=D("2.20"), action="Sell to Open", qty=1):
    from meic.domain.events import ExternalFillImported

    return ExternalFillImported(
        day="2026-07-21", at="2026-07-21T15:29:00-04:00", order_id="1",
        symbol="whatever", action=action, quantity=qty, price=price, fee=None,
        imported_at="2026-07-21T16:00:00-04:00", source="tastytrade_history",
        underlying=underlying)


def test_imported_fill_dollars_scales_an_es_fill_at_x50_not_x100():
    from meic.reporting.folds import imported_fill_dollars

    es_fill = _imported(underlying="/ES", price=D("2.20"))
    assert imported_fill_dollars(es_fill) == D("110.00")   # 2.20 * 50, never * 100


def test_imported_fill_dollars_default_underlying_keeps_the_old_x100_math():
    """Byte-identity: an ExternalFillImported constructed the pre-v1.86 way
    (no `underlying` kwarg at all) still values at x100 -- the SPX/RUT
    behaviour proven in tests/reporting/test_folds.py is unchanged."""
    from meic.domain.events import ExternalFillImported
    from meic.reporting.folds import imported_fill_dollars

    fill = ExternalFillImported(
        day="2026-07-09", at="2026-07-09T15:29:00-04:00", order_id="482390058",
        symbol="SPXW  260709P07535000", action="Sell to Open", quantity=1,
        price=D("2.20"), fee=D("1.22"),
        imported_at="2026-07-10T09:00:00-04:00", source="tastytrade_history")
    assert imported_fill_dollars(fill) == D("220.00")


def test_imported_fill_dollars_settlement_rows_are_unaffected_by_underlying():
    """A settlement row's `value` is already the broker's own net cash effect
    -- no multiplier of any kind applies, /ES included."""
    from meic.domain.events import ExternalFillImported
    from meic.reporting.folds import imported_fill_dollars

    fill = ExternalFillImported(
        day="2026-07-21", at="2026-07-21T15:29:00-04:00", order_id="1",
        symbol="whatever", action="Cash Settled Assignment", quantity=1,
        price=None, fee=D("5.00"), value=D("-369.00"),
        imported_at="2026-07-21T16:00:00-04:00", source="tastytrade_history",
        underlying="/ES")
    assert imported_fill_dollars(fill) == D("-369.00")


def test_external_fill_imported_underlying_round_trips_through_the_event_codec():
    # price=None here sidesteps a pre-existing, unrelated codec quirk: a
    # `Decimal | None`-typed field (post `from __future__ import annotations`,
    # `field.type` is the STRING "Decimal | None") never matches the codec's
    # `f.type in ("Decimal", Decimal)` branch, so a non-None price round-trips
    # as a str, not a Decimal -- out of scope for this additive field's test.
    fill = _imported(underlying="/ES", price=None)
    from meic.domain.events import Event

    revived = Event.from_dict(fill.to_dict())
    assert revived == fill and revived.underlying == "/ES"

    # pre-v1.86 shape: field absent entirely -> defaults to SPX on replay.
    legacy_dict = fill.to_dict()
    del legacy_dict["underlying"]
    legacy = Event.from_dict(legacy_dict)
    assert legacy.underlying == "SPX"
