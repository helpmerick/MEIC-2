"""FEATURE 3: live P/L enricher — the math, unit-tested against a fake chain
snapshot (no DXLink, no broker). server.py wires this over the SAME snapshot
selection already takes (`_Snapshots.last`) — no new subscription.

NFR-04 (2026-07-13): the hub-first / snapshot-fallback resolution tests below
prove the "strictly no worse than today" safety property -- a fresh QuoteHub
mark is preferred and stamps `live_pnl_asof` with its OWN timestamp; a stale
or absent hub mark falls through to the EXACT snapshot path this replaced,
pinned against the pre-NFR-04 numbers above.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

from meic.adapters.api.server import _live_pnl_enricher
from meic.adapters.dxlink.chain_snapshot import ChainSnapshot
from meic.domain.chain import ChainSide, Mark
from meic.domain.staleness import StampedQuote

TAKEN_AT = datetime(2026, 7, 9, 14, 35, tzinfo=timezone.utc)

# The journaled/broker OCC symbols (ORD-09) the entry legs carry -- note the
# double space, exactly as the broker reports them. NOT subscribable on DXLink.
PUT_SHORT_SYM = "SPXW  260709P07535000"
PUT_LONG_SYM = "SPXW  260709P07510000"
CALL_SHORT_SYM = "SPXW  260709C07540000"
CALL_LONG_SYM = "SPXW  260709C07565000"

# The DXFEED STREAMER symbols the SAME strikes are quoted under -- the only
# namespace DXLink will send, and therefore the namespace the QuoteHub is keyed
# by (NFR-04). Deliberately distinct, recognisable strings so a test can assert
# the OCC form never leaks into a subscription or a hub lookup.
PUT_SHORT_STREAMER = ".SPXW260709P7535"
PUT_LONG_STREAMER = ".SPXW260709P7510"
CALL_SHORT_STREAMER = ".SPXW260709C7540"
CALL_LONG_STREAMER = ".SPXW260709C7565"

# strike -> (put_streamer, call_streamer), the shape `ChainSnapshot.streamer_symbols`
# carries (adapters/dxlink/chain_snapshot.py).
STREAMER_MAP = {
    D("7535"): (PUT_SHORT_STREAMER, ".SPXW260709C7535"),
    D("7510"): (PUT_LONG_STREAMER, ".SPXW260709C7510"),
    D("7540"): (".SPXW260709P7540", CALL_SHORT_STREAMER),
    D("7565"): (".SPXW260709P7565", CALL_LONG_STREAMER),
}


class _Snaps:
    """Minimal stand-in for server.py's `_Snapshots` holder."""

    def __init__(self, last=None):
        self.last = last


class _FakeHub:
    """Minimal stand-in for `QuoteHub.mark` -- a symbol -> StampedQuote map."""

    def __init__(self, marks: dict[str, StampedQuote] | None = None):
        self._marks = marks or {}

    def mark(self, symbol):
        return self._marks.get(symbol)


class _FakeClock:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now


def _snapshot(put_marks: dict, call_marks: dict, *, stale: bool = False,
              streamer_symbols: dict | None = None) -> ChainSnapshot:
    return ChainSnapshot(
        spot=D("7540"), expiration=date(2026, 7, 9),
        put_side=ChainSide(strikes_toward_otm=tuple(sorted(put_marks, reverse=True)), marks=put_marks),
        call_side=ChainSide(strikes_toward_otm=tuple(sorted(call_marks)), marks=call_marks),
        put_band=(), call_band=(), symbols={},
        taken_at=TAKEN_AT, stale=stale,
        streamer_symbols=STREAMER_MAP if streamer_symbols is None else streamer_symbols)


def _leg(side, role, strike, price, qty=1, symbol=None):
    leg = {"side": side, "role": role, "strike": strike, "price": price, "qty": qty}
    if symbol is not None:
        leg["symbol"] = symbol
    return leg


def _card(legs, *, status="PROTECTED", net_credit="3.60"):
    return {"status": status, "net_credit": net_credit, "legs": legs}


FULL_LEGS = [
    _leg("PUT", "short", "7535", "1.80"),
    _leg("PUT", "long", "7510", "0.08"),
    _leg("CALL", "short", "7540", "1.95"),
    _leg("CALL", "long", "7565", "0.07"),
]


def test_live_pnl_computed_when_every_mark_is_present():
    put_marks = {D("7535"): Mark(bid=D("1.65"), ask=D("1.75")),   # mid 1.70
                D("7510"): Mark(bid=D("0.07"), ask=D("0.09"))}   # mid 0.08
    call_marks = {D("7540"): Mark(bid=D("1.90"), ask=D("2.00")),  # mid 1.95
                 D("7565"): Mark(bid=D("0.06"), ask=D("0.08"))}  # mid 0.07
    snap = _snapshot(put_marks, call_marks)
    enrich = _live_pnl_enricher(_Snaps(snap))

    cards = enrich([_card(FULL_LEGS)])

    # current_value = (1.70-0.08) + (1.95-0.07) = 1.62 + 1.88 = 3.50
    # live_pnl = (3.60 - 3.50) x 100 x 1 = 10.00
    assert cards[0]["live_pnl"] == "10.00"
    assert cards[0]["live_pnl_asof"] == TAKEN_AT.isoformat()


def test_live_pnl_is_null_when_one_mark_is_missing():
    """A strike outside the ATM band (or simply unquoted) yields an honest '—',
    never a guess."""
    put_marks = {D("7535"): Mark(bid=D("1.65"), ask=D("1.75"))}   # 7510 UNMARKED
    call_marks = {D("7540"): Mark(bid=D("1.90"), ask=D("2.00")),
                 D("7565"): Mark(bid=D("0.06"), ask=D("0.08"))}
    snap = _snapshot(put_marks, call_marks)
    enrich = _live_pnl_enricher(_Snaps(snap))

    cards = enrich([_card(FULL_LEGS)])

    assert cards[0]["live_pnl"] is None
    assert cards[0]["live_pnl_asof"] is None


def test_live_pnl_is_null_when_the_snapshot_is_stale():
    put_marks = {D("7535"): Mark(bid=D("1.65"), ask=D("1.75")), D("7510"): Mark(bid=D("0.07"), ask=D("0.09"))}
    call_marks = {D("7540"): Mark(bid=D("1.90"), ask=D("2.00")), D("7565"): Mark(bid=D("0.06"), ask=D("0.08"))}
    snap = _snapshot(put_marks, call_marks, stale=True)
    enrich = _live_pnl_enricher(_Snaps(snap))

    cards = enrich([_card(FULL_LEGS)])

    assert cards[0]["live_pnl"] is None and cards[0]["live_pnl_asof"] is None


def test_live_pnl_is_null_when_no_snapshot_has_ever_been_taken():
    enrich = _live_pnl_enricher(_Snaps(None))

    cards = enrich([_card(FULL_LEGS)])

    assert cards[0]["live_pnl"] is None and cards[0]["live_pnl_asof"] is None


def test_live_pnl_skips_terminal_and_legless_cards():
    put_marks = {D("7535"): Mark(bid=D("1.65"), ask=D("1.75")), D("7510"): Mark(bid=D("0.07"), ask=D("0.09"))}
    call_marks = {D("7540"): Mark(bid=D("1.90"), ask=D("2.00")), D("7565"): Mark(bid=D("0.06"), ask=D("0.08"))}
    snap = _snapshot(put_marks, call_marks)
    enrich = _live_pnl_enricher(_Snaps(snap))

    closed = _card(FULL_LEGS, status="CLOSED")
    legless = _card([], status="PROTECTED")
    cards = enrich([closed, legless])

    assert cards[0]["live_pnl"] is None and cards[1]["live_pnl"] is None


# --- NFR-04: QuoteHub live-first / snapshot-fallback resolution -------------

# Same strikes/prices as FULL_LEGS above, PLUS the broker OCC symbol each leg
# carries (ORD-09). The hub, however, is keyed by STREAMER symbol -- the
# enricher must translate. This asymmetry IS the bug these tests pin.
FULL_LEGS_WITH_SYMBOLS = [
    _leg("PUT", "short", "7535", "1.80", symbol=PUT_SHORT_SYM),
    _leg("PUT", "long", "7510", "0.08", symbol=PUT_LONG_SYM),
    _leg("CALL", "short", "7540", "1.95", symbol=CALL_SHORT_SYM),
    _leg("CALL", "long", "7565", "0.07", symbol=CALL_LONG_SYM),
]

# The SAME snapshot-only marks/result as test_live_pnl_computed_when_every_mark_is_present
# above (mid 1.70/0.08/1.95/0.07 -> current_value 3.50 -> live_pnl "10.00") --
# the pinned "strictly no worse than today" baseline every NFR-04 test below
# is checked against.
_SNAPSHOT_PUT_MARKS = {D("7535"): Mark(bid=D("1.65"), ask=D("1.75")), D("7510"): Mark(bid=D("0.07"), ask=D("0.09"))}
_SNAPSHOT_CALL_MARKS = {D("7540"): Mark(bid=D("1.90"), ask=D("2.00")), D("7565"): Mark(bid=D("0.06"), ask=D("0.08"))}
_BASELINE_LIVE_PNL = "10.00"


def _streamer_keyed_hub(at, *, put_short=("1.65", "1.75"), put_long=("0.07", "0.09"),
                        call_short=("1.90", "2.00"), call_long=("0.06", "0.08"),
                        omit=()) -> _FakeHub:
    """A hub keyed exactly as the live one is: by STREAMER symbol. The legs
    themselves carry OCC symbols, so a lookup that forgets to translate finds
    nothing -- which is precisely how the first cut shipped an always-empty
    hub."""
    marks = {
        PUT_SHORT_STREAMER: StampedQuote(PUT_SHORT_STREAMER, D(put_short[0]), D(put_short[1]), at),
        PUT_LONG_STREAMER: StampedQuote(PUT_LONG_STREAMER, D(put_long[0]), D(put_long[1]), at),
        CALL_SHORT_STREAMER: StampedQuote(CALL_SHORT_STREAMER, D(call_short[0]), D(call_short[1]), at),
        CALL_LONG_STREAMER: StampedQuote(CALL_LONG_STREAMER, D(call_long[0]), D(call_long[1]), at),
    }
    for sym in omit:
        marks.pop(sym, None)
    return _FakeHub(marks)


def test_nfr04_fresh_hub_marks_are_used_and_asof_reflects_the_hub_stamp():
    """A fresh hub tick for every leg is preferred over the (identical, here)
    snapshot marks -- and `live_pnl_asof` reflects the HUB quote's own
    timestamp, not the snapshot's `taken_at`, proving the operator actually
    sees the mark move.

    THE REGRESSION THIS PINS: the hub is keyed by STREAMER symbol while the
    legs carry OCC. The first cut looked up `hub.mark(<OCC>)`, found nothing
    for every leg, and silently fell back to the snapshot forever -- a hub full
    of ticks the enricher could never find."""
    snap = _snapshot(_SNAPSHOT_PUT_MARKS, _SNAPSHOT_CALL_MARKS)
    hub_at = TAKEN_AT + timedelta(seconds=45)   # long after the snapshot was taken
    now = hub_at + timedelta(milliseconds=200)  # well within the freshness bar
    hub = _streamer_keyed_hub(hub_at)
    enrich = _live_pnl_enricher(_Snaps(snap), hub, clock=_FakeClock(now), max_quote_age_ms=3000)

    cards = enrich([_card(FULL_LEGS_WITH_SYMBOLS)])

    assert cards[0]["live_pnl"] == _BASELINE_LIVE_PNL
    assert cards[0]["live_pnl_asof"] == hub_at.isoformat()
    assert cards[0]["live_pnl_asof"] != snap.taken_at.isoformat()


def test_nfr04_hub_lookup_uses_the_streamer_symbol_not_the_occ_symbol():
    """Explicit namespace guard: the hub holds a mark ONLY under the streamer
    symbol, and a DIFFERENT (wrong, deliberately mispriced) mark under the OCC
    string. If the enricher ever looks up by OCC it will read the junk and the
    P/L will be wrong -- so this pins which key is actually used."""
    snap = _snapshot(_SNAPSHOT_PUT_MARKS, _SNAPSHOT_CALL_MARKS)
    hub_at = TAKEN_AT + timedelta(seconds=30)
    now = hub_at + timedelta(milliseconds=100)
    marks = {
        PUT_SHORT_STREAMER: StampedQuote(PUT_SHORT_STREAMER, D("1.65"), D("1.75"), hub_at),
        PUT_LONG_STREAMER: StampedQuote(PUT_LONG_STREAMER, D("0.07"), D("0.09"), hub_at),
        CALL_SHORT_STREAMER: StampedQuote(CALL_SHORT_STREAMER, D("1.90"), D("2.00"), hub_at),
        CALL_LONG_STREAMER: StampedQuote(CALL_LONG_STREAMER, D("0.06"), D("0.08"), hub_at),
        # Poison: if anything resolves by the OCC string, the total goes wrong.
        PUT_SHORT_SYM: StampedQuote(PUT_SHORT_SYM, D("9.00"), D("9.10"), hub_at),
        CALL_SHORT_SYM: StampedQuote(CALL_SHORT_SYM, D("9.00"), D("9.10"), hub_at),
    }
    enrich = _live_pnl_enricher(_Snaps(snap), _FakeHub(marks),
                                clock=_FakeClock(now), max_quote_age_ms=3000)

    cards = enrich([_card(FULL_LEGS_WITH_SYMBOLS)])

    assert cards[0]["live_pnl"] == _BASELINE_LIVE_PNL   # streamer-keyed marks, never the OCC poison
    assert cards[0]["live_pnl_asof"] == hub_at.isoformat()


def test_nfr04_leg_strike_absent_from_the_streamer_map_falls_back_to_snapshot():
    """A leg whose strike is outside the subscribed span has NO streamer symbol
    -- it must not be looked up (and must never have one guessed for it); it
    resolves off the snapshot exactly as today, with no crash."""
    partial_map = {k: v for k, v in STREAMER_MAP.items() if k != D("7565")}  # call long dropped
    snap = _snapshot(_SNAPSHOT_PUT_MARKS, _SNAPSHOT_CALL_MARKS, streamer_symbols=partial_map)
    hub_at = TAKEN_AT + timedelta(seconds=30)
    now = hub_at + timedelta(milliseconds=100)
    hub = _streamer_keyed_hub(hub_at)
    enrich = _live_pnl_enricher(_Snaps(snap), hub, clock=_FakeClock(now), max_quote_age_ms=3000)

    cards = enrich([_card(FULL_LEGS_WITH_SYMBOLS)])

    # Three legs live + the call long off the snapshot -> same pinned total,
    # and (a mixed-source card) the snapshot's own asof.
    assert cards[0]["live_pnl"] == _BASELINE_LIVE_PNL
    assert cards[0]["live_pnl_asof"] == snap.taken_at.isoformat()


def test_nfr04_no_streamer_map_at_all_falls_back_to_snapshot():
    """A snapshot with an EMPTY streamer map (older shape / never populated):
    nothing is translatable, so nothing is looked up -- byte-identical to
    today's snapshot-only behaviour."""
    snap = _snapshot(_SNAPSHOT_PUT_MARKS, _SNAPSHOT_CALL_MARKS, streamer_symbols={})
    hub_at = TAKEN_AT + timedelta(seconds=30)
    hub = _streamer_keyed_hub(hub_at)
    enrich = _live_pnl_enricher(_Snaps(snap), hub, clock=_FakeClock(hub_at + timedelta(milliseconds=100)))

    cards = enrich([_card(FULL_LEGS_WITH_SYMBOLS)])

    assert cards[0]["live_pnl"] == _BASELINE_LIVE_PNL
    assert cards[0]["live_pnl_asof"] == TAKEN_AT.isoformat()


def test_nfr04_stale_hub_mark_falls_back_to_the_snapshot_value():
    """A hub mark older than `max_quote_age_ms` is treated as ABSENT -- the
    leg falls through to the exact snapshot mid, and `live_pnl_asof` stays
    the snapshot's own `taken_at` (never a stale hub timestamp dressed up as
    live)."""
    snap = _snapshot(_SNAPSHOT_PUT_MARKS, _SNAPSHOT_CALL_MARKS)
    now = TAKEN_AT + timedelta(seconds=10)
    stale_at = now - timedelta(milliseconds=4000)  # older than the 3000ms bar
    # streamer-keyed (as the live hub is) AND deliberately mispriced: if a stale
    # mark were ever used, the total would be visibly wrong.
    hub = _streamer_keyed_hub(stale_at, put_short=("9.00", "9.10"))
    enrich = _live_pnl_enricher(_Snaps(snap), hub, clock=_FakeClock(now), max_quote_age_ms=3000)

    cards = enrich([_card(FULL_LEGS_WITH_SYMBOLS)])

    assert cards[0]["live_pnl"] == _BASELINE_LIVE_PNL   # snapshot mids used, not the stale/corrupt hub ones
    assert cards[0]["live_pnl_asof"] == snap.taken_at.isoformat()


def test_nfr04_empty_hub_is_byte_identical_to_the_pre_wiring_snapshot_path():
    """STRICTLY NO WORSE proof: a hub with no marks at all for these symbols
    (down/never-started/sick) reproduces the EXACT pinned numbers from
    `test_live_pnl_computed_when_every_mark_is_present` above -- no
    regression is possible."""
    snap = _snapshot(_SNAPSHOT_PUT_MARKS, _SNAPSHOT_CALL_MARKS)
    hub = _FakeHub({})   # nothing landed
    enrich = _live_pnl_enricher(_Snaps(snap), hub, clock=_FakeClock(TAKEN_AT + timedelta(seconds=30)))

    cards = enrich([_card(FULL_LEGS_WITH_SYMBOLS)])

    assert cards[0]["live_pnl"] == _BASELINE_LIVE_PNL
    assert cards[0]["live_pnl_asof"] == TAKEN_AT.isoformat()


def test_nfr04_no_hub_at_all_is_byte_identical_to_the_pre_wiring_snapshot_path():
    """The SAME proof as above, but with `hub=None`/no `clock` -- the exact
    call shape every pre-NFR-04 caller uses -- so an un-migrated caller (or a
    hub never constructed) is unaffected."""
    snap = _snapshot(_SNAPSHOT_PUT_MARKS, _SNAPSHOT_CALL_MARKS)
    enrich = _live_pnl_enricher(_Snaps(snap))

    cards = enrich([_card(FULL_LEGS_WITH_SYMBOLS)])

    assert cards[0]["live_pnl"] == _BASELINE_LIVE_PNL
    assert cards[0]["live_pnl_asof"] == TAKEN_AT.isoformat()


def test_nfr04_neither_hub_nor_snapshot_mark_is_an_honest_none():
    """A leg unmarked in BOTH the hub and the snapshot never fabricates a
    number."""
    put_marks = {D("7535"): Mark(bid=D("1.65"), ask=D("1.75"))}   # 7510 UNMARKED in the snapshot too
    snap = _snapshot(put_marks, _SNAPSHOT_CALL_MARKS)
    hub = _FakeHub({})  # and nothing in the hub either
    enrich = _live_pnl_enricher(_Snaps(snap), hub, clock=_FakeClock(TAKEN_AT + timedelta(seconds=5)))

    cards = enrich([_card(FULL_LEGS_WITH_SYMBOLS)])

    assert cards[0]["live_pnl"] is None
    assert cards[0]["live_pnl_asof"] is None


def test_nfr04_mixed_sources_do_not_claim_a_fully_live_asof():
    """Three legs fresh in the hub, one falling back to the snapshot: the
    live_pnl NUMBER still blends both sources (best available mark per leg),
    but `live_pnl_asof` must NOT claim the hub's timestamp -- a partially-live
    card is stamped with the snapshot's own `taken_at`, never a misleadingly
    fresh-looking asof for data that is only partly live."""
    snap = _snapshot(_SNAPSHOT_PUT_MARKS, _SNAPSHOT_CALL_MARKS)
    hub_at = TAKEN_AT + timedelta(seconds=20)
    now = hub_at + timedelta(milliseconds=100)
    # the call long never ticked -> must fall back to the snapshot mid
    hub = _streamer_keyed_hub(hub_at, omit=(CALL_LONG_STREAMER,))
    enrich = _live_pnl_enricher(_Snaps(snap), hub, clock=_FakeClock(now), max_quote_age_ms=3000)

    cards = enrich([_card(FULL_LEGS_WITH_SYMBOLS)])

    assert cards[0]["live_pnl"] == _BASELINE_LIVE_PNL
    assert cards[0]["live_pnl_asof"] == snap.taken_at.isoformat()
