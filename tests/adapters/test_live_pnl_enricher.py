"""FEATURE 3: live P/L enricher — the math, unit-tested against a fake chain
snapshot (no DXLink, no broker). server.py wires this over the SAME snapshot
selection already takes (`_Snapshots.last`) — no new subscription.

BUG FIX (2026-07-13, live incident): `_live_pnl_enricher` used to re-mark ALL
FOUR legs as if the whole condor were still open, ignoring `stop_fills`/
`recoveries`/`fees` — so a stopped-and-closed side priced a spread the bot no
longer owned. It now derives `live_pnl` from the SAME per-share quantity
(`domain.tpf.entry_profit_amount`) and the SAME open-side costing
(`_open_side_costs`) that the shared TPF/TPT evaluator (`_entry_profit_pct_now`)
uses — one formula, two consumers — so `live_pnl` and `profit_pct` can never
diverge. That means the enricher now needs the projection entry itself (for
`stop_fills`/`recoveries`/`fees`/`sides_stopped`/etc.), read the SAME way
`_profit_pct_enricher` already does: `fold(comp.events).entries[entry_id]`.

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
from meic.domain.events import CondorFilled, FilledLeg, LongSold, ShortStopped
from meic.domain.staleness import StampedQuote
from meic.domain.tpf import entry_profit_pct

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


class _Comp:
    """Minimal stand-in for the composition object: just enough for
    `fold(comp.events)` -- the SAME shape `_profit_pct_enricher` reads."""

    def __init__(self, events):
        self.events = events


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


def _leg(side, role, symbol, price, qty=1):
    return FilledLeg(symbol=symbol, right="P" if side == "PUT" else "C", role=role,
                     qty=qty, price=D(price))


FULL_LEGS = (
    _leg("PUT", "short", PUT_SHORT_SYM, "1.80"),
    _leg("PUT", "long", PUT_LONG_SYM, "0.08"),
    _leg("CALL", "short", CALL_SHORT_SYM, "1.95"),
    _leg("CALL", "long", CALL_LONG_SYM, "0.07"),
)


def _filled(entry_id, *, net_credit="3.60", legs=FULL_LEGS, fee="0"):
    return CondorFilled(at=TAKEN_AT.isoformat(), entry_id=entry_id, net_credit=D(net_credit),
                        fee=D(fee), legs=legs)


def _card(entry_id, *, status="PROTECTED"):
    return {"entry_id": entry_id, "status": status}


def test_live_pnl_computed_when_every_mark_is_present():
    put_marks = {D("7535"): Mark(bid=D("1.65"), ask=D("1.75")),   # mid 1.70
                D("7510"): Mark(bid=D("0.07"), ask=D("0.09"))}   # mid 0.08
    call_marks = {D("7540"): Mark(bid=D("1.90"), ask=D("2.00")),  # mid 1.95
                 D("7565"): Mark(bid=D("0.06"), ask=D("0.08"))}  # mid 0.07
    snap = _snapshot(put_marks, call_marks)
    comp = _Comp([_filled("e1")])
    enrich = _live_pnl_enricher(comp, _Snaps(snap))

    cards = enrich([_card("e1")])

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
    comp = _Comp([_filled("e1")])
    enrich = _live_pnl_enricher(comp, _Snaps(snap))

    cards = enrich([_card("e1")])

    assert cards[0]["live_pnl"] is None
    assert cards[0]["live_pnl_asof"] is None


def test_live_pnl_is_null_when_the_snapshot_is_stale():
    put_marks = {D("7535"): Mark(bid=D("1.65"), ask=D("1.75")), D("7510"): Mark(bid=D("0.07"), ask=D("0.09"))}
    call_marks = {D("7540"): Mark(bid=D("1.90"), ask=D("2.00")), D("7565"): Mark(bid=D("0.06"), ask=D("0.08"))}
    snap = _snapshot(put_marks, call_marks, stale=True)
    comp = _Comp([_filled("e1")])
    enrich = _live_pnl_enricher(comp, _Snaps(snap))

    cards = enrich([_card("e1")])

    assert cards[0]["live_pnl"] is None and cards[0]["live_pnl_asof"] is None


def test_live_pnl_is_null_when_no_snapshot_has_ever_been_taken():
    comp = _Comp([_filled("e1")])
    enrich = _live_pnl_enricher(comp, _Snaps(None))

    cards = enrich([_card("e1")])

    assert cards[0]["live_pnl"] is None and cards[0]["live_pnl_asof"] is None


def test_live_pnl_skips_terminal_and_legless_cards():
    put_marks = {D("7535"): Mark(bid=D("1.65"), ask=D("1.75")), D("7510"): Mark(bid=D("0.07"), ask=D("0.09"))}
    call_marks = {D("7540"): Mark(bid=D("1.90"), ask=D("2.00")), D("7565"): Mark(bid=D("0.06"), ask=D("0.08"))}
    snap = _snapshot(put_marks, call_marks)
    # "e1" is CLOSED; "e2" has no CondorFilled event at all (no legs ever
    # recorded) -- both must yield an honest null, never a guess.
    comp = _Comp([_filled("e1")])
    enrich = _live_pnl_enricher(comp, _Snaps(snap))

    closed = _card("e1", status="CLOSED")
    legless = _card("e2", status="PROTECTED")
    cards = enrich([closed, legless])

    assert cards[0]["live_pnl"] is None and cards[1]["live_pnl"] is None


# --- THE REGRESSION: a stopped+recovered side must not be re-marked --------

def test_stopped_and_recovered_put_side_is_not_remarked_call_side_only_priced():
    """THE LIVE BUG (2026-07-13): the PUT side stopped (fill 2.95) and was
    LEX-recovered (0.65); the CALL side is still open, credit 2.80. The old
    formula re-marked ALL FOUR legs including the closed PUT spread -- pure
    fiction, since the bot no longer owns those legs. The fix must price
    ONLY the open CALL side and fold the PUT side's REALIZED fills."""
    call_marks = {D("7540"): Mark(bid=D("1.90"), ask=D("2.10")),   # mid 2.00
                 D("7565"): Mark(bid=D("1.10"), ask=D("1.30"))}   # mid 1.20
    # PUT marks are present in the snapshot but must be COMPLETELY IGNORED --
    # the side is closed, so re-marking it would be exactly the bug.
    put_marks = {D("7535"): Mark(bid=D("50.00"), ask=D("60.00")),
                D("7510"): Mark(bid=D("0.01"), ask=D("0.02"))}
    snap = _snapshot(put_marks, call_marks)
    comp = _Comp([
        _filled("e1", net_credit="2.80"),
        ShortStopped(entry_id="e1", side="PUT", fill=D("2.95"), slippage=D("0")),
        LongSold(entry_id="e1", side="PUT", recovery=D("0.65")),
    ])
    enrich = _live_pnl_enricher(comp, _Snaps(snap))

    cards = enrich([_card("e1", status="LEX_RECOVERED")])

    # call cost-to-close = 2.00 - 1.20 = 0.80
    # realized = net_credit - fees - stop_fills + recoveries = 2.80 - 0 - 2.95 + 0.65 = 0.50
    # profit (per-share) = realized - open_cost = 0.50 - 0.80 = -0.30
    # live_pnl = -0.30 * 100 * 1 = -30.00
    assert cards[0]["live_pnl"] == "-30.00"

    # THE BUGGY all-four-legs re-mark would have been:
    #   current_value = (50.00-put-short-mid=55 - 0.015) + (2.00 - 1.20)
    # -- wildly different and not what's asserted above; this pins the fix
    # never falls back to that computation.
    buggy_current_value = (D("55.00") - D("0.015")) + (D("2.00") - D("1.20"))
    buggy_live_pnl = (D("2.80") - buggy_current_value) * 100 * 1
    assert D(cards[0]["live_pnl"]) != buggy_live_pnl


def test_live_pnl_and_profit_pct_agree_by_construction_stopped_and_recovered():
    """`live_pnl` and `profit_pct` must derive from the SAME formula: dividing
    `live_pnl` back down to a percentage must equal `profit_pct` exactly, for
    every combination of nothing-closed / one-side-stopped / stopped+recovered."""
    call_marks = {D("7540"): Mark(bid=D("1.90"), ask=D("2.10")), D("7565"): Mark(bid=D("1.10"), ask=D("1.30"))}
    put_marks = {D("7535"): Mark(bid=D("1.65"), ask=D("1.75")), D("7510"): Mark(bid=D("0.07"), ask=D("0.09"))}
    snap = _snapshot(put_marks, call_marks)
    net_credit = D("2.80")

    scenarios = [
        ("nothing_closed", []),
        ("one_side_stopped", [ShortStopped(entry_id="e1", side="PUT", fill=D("2.95"), slippage=D("0"))]),
        ("one_side_stopped_and_recovered", [
            ShortStopped(entry_id="e1", side="PUT", fill=D("2.95"), slippage=D("0")),
            LongSold(entry_id="e1", side="PUT", recovery=D("0.65")),
        ]),
    ]
    for _name, extra_events in scenarios:
        comp = _Comp([_filled("e1", net_credit=str(net_credit)), *extra_events])
        enrich = _live_pnl_enricher(comp, _Snaps(snap))
        cards = enrich([_card("e1")])

        live_pnl = D(cards[0]["live_pnl"])
        contracts = 1
        computed_pct = live_pnl / (net_credit * 100 * contracts) * 100

        e = comp.events  # re-derive profit_pct the same way the app does
        from meic.domain.projection import fold
        entry = fold(e).entries["e1"]
        from meic.adapters.api.server import _open_side_costs
        open_costs = _open_side_costs(entry, snap)
        expected_pct = entry_profit_pct(net_credit=entry.net_credit, fees=entry.fees,
                                        stop_fills=entry.stop_fills, recoveries=entry.recoveries,
                                        open_side_costs=open_costs)
        assert computed_pct == expected_pct, _name


def test_live_pnl_is_a_real_number_when_both_sides_are_closed():
    """Both sides stopped -- nothing left open, so NO mark is required at
    all; the entry still produces a live P&L purely from realized fills."""
    snap = _snapshot({}, {})  # no chain marks at all
    comp = _Comp([
        _filled("e1", net_credit="2.80"),
        ShortStopped(entry_id="e1", side="PUT", fill=D("2.95"), slippage=D("0")),
        LongSold(entry_id="e1", side="PUT", recovery=D("0.65")),
        ShortStopped(entry_id="e1", side="CALL", fill=D("1.50"), slippage=D("0")),
        LongSold(entry_id="e1", side="CALL", recovery=D("0.20")),
    ])
    enrich = _live_pnl_enricher(comp, _Snaps(snap))

    cards = enrich([_card("e1", status="LEX_RECOVERED")])

    # realized = 2.80 - 0 - (2.95+1.50) + (0.65+0.20) = 2.80 - 4.45 + 0.85 = -0.80
    assert cards[0]["live_pnl"] == "-80.00"
    assert cards[0]["live_pnl_asof"] == TAKEN_AT.isoformat()


def test_live_pnl_is_null_when_one_side_closed_and_the_open_sides_mark_is_missing():
    """One side stopped+recovered; the remaining OPEN side has NO quote in
    either the hub or the snapshot -- honest null, not a guess."""
    put_marks = {}  # PUT is closed anyway, irrelevant
    call_marks = {D("7540"): Mark(bid=D("1.90"), ask=D("2.10"))}  # 7565 (call long) UNMARKED
    snap = _snapshot(put_marks, call_marks)
    comp = _Comp([
        _filled("e1", net_credit="2.80"),
        ShortStopped(entry_id="e1", side="PUT", fill=D("2.95"), slippage=D("0")),
        LongSold(entry_id="e1", side="PUT", recovery=D("0.65")),
    ])
    enrich = _live_pnl_enricher(comp, _Snaps(snap))

    cards = enrich([_card("e1", status="LEX_RECOVERED")])

    assert cards[0]["live_pnl"] is None
    assert cards[0]["live_pnl_asof"] is None


# --- NFR-04: QuoteHub live-first / snapshot-fallback resolution -------------

# Same strikes/prices as FULL_LEGS above -- the hub, however, is keyed by
# STREAMER symbol -- the enricher must translate. This asymmetry IS the bug
# these tests pin.

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
    comp = _Comp([_filled("e1")])
    enrich = _live_pnl_enricher(comp, _Snaps(snap), hub, clock=_FakeClock(now), max_quote_age_ms=3000)

    cards = enrich([_card("e1")])

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
    comp = _Comp([_filled("e1")])
    enrich = _live_pnl_enricher(comp, _Snaps(snap), _FakeHub(marks),
                                clock=_FakeClock(now), max_quote_age_ms=3000)

    cards = enrich([_card("e1")])

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
    comp = _Comp([_filled("e1")])
    enrich = _live_pnl_enricher(comp, _Snaps(snap), hub, clock=_FakeClock(now), max_quote_age_ms=3000)

    cards = enrich([_card("e1")])

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
    comp = _Comp([_filled("e1")])
    enrich = _live_pnl_enricher(comp, _Snaps(snap), hub, clock=_FakeClock(hub_at + timedelta(milliseconds=100)))

    cards = enrich([_card("e1")])

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
    comp = _Comp([_filled("e1")])
    enrich = _live_pnl_enricher(comp, _Snaps(snap), hub, clock=_FakeClock(now), max_quote_age_ms=3000)

    cards = enrich([_card("e1")])

    assert cards[0]["live_pnl"] == _BASELINE_LIVE_PNL   # snapshot mids used, not the stale/corrupt hub ones
    assert cards[0]["live_pnl_asof"] == snap.taken_at.isoformat()


def test_nfr04_empty_hub_is_byte_identical_to_the_pre_wiring_snapshot_path():
    """STRICTLY NO WORSE proof: a hub with no marks at all for these symbols
    (down/never-started/sick) reproduces the EXACT pinned numbers from
    `test_live_pnl_computed_when_every_mark_is_present` above -- no
    regression is possible."""
    snap = _snapshot(_SNAPSHOT_PUT_MARKS, _SNAPSHOT_CALL_MARKS)
    hub = _FakeHub({})   # nothing landed
    comp = _Comp([_filled("e1")])
    enrich = _live_pnl_enricher(comp, _Snaps(snap), hub, clock=_FakeClock(TAKEN_AT + timedelta(seconds=30)))

    cards = enrich([_card("e1")])

    assert cards[0]["live_pnl"] == _BASELINE_LIVE_PNL
    assert cards[0]["live_pnl_asof"] == TAKEN_AT.isoformat()


def test_nfr04_no_hub_at_all_is_byte_identical_to_the_pre_wiring_snapshot_path():
    """The SAME proof as above, but with `hub=None`/no `clock` -- the exact
    call shape every pre-NFR-04 caller uses -- so an un-migrated caller (or a
    hub never constructed) is unaffected."""
    snap = _snapshot(_SNAPSHOT_PUT_MARKS, _SNAPSHOT_CALL_MARKS)
    comp = _Comp([_filled("e1")])
    enrich = _live_pnl_enricher(comp, _Snaps(snap))

    cards = enrich([_card("e1")])

    assert cards[0]["live_pnl"] == _BASELINE_LIVE_PNL
    assert cards[0]["live_pnl_asof"] == TAKEN_AT.isoformat()


def test_nfr04_neither_hub_nor_snapshot_mark_is_an_honest_none():
    """A leg unmarked in BOTH the hub and the snapshot never fabricates a
    number."""
    put_marks = {D("7535"): Mark(bid=D("1.65"), ask=D("1.75"))}   # 7510 UNMARKED in the snapshot too
    snap = _snapshot(put_marks, _SNAPSHOT_CALL_MARKS)
    hub = _FakeHub({})  # and nothing in the hub either
    comp = _Comp([_filled("e1")])
    enrich = _live_pnl_enricher(comp, _Snaps(snap), hub, clock=_FakeClock(TAKEN_AT + timedelta(seconds=5)))

    cards = enrich([_card("e1")])

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
    comp = _Comp([_filled("e1")])
    enrich = _live_pnl_enricher(comp, _Snaps(snap), hub, clock=_FakeClock(now), max_quote_age_ms=3000)

    cards = enrich([_card("e1")])

    assert cards[0]["live_pnl"] == _BASELINE_LIVE_PNL
    assert cards[0]["live_pnl_asof"] == snap.taken_at.isoformat()
