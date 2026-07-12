"""FEATURE 3: live P/L enricher — the math, unit-tested against a fake chain
snapshot (no DXLink, no broker). server.py wires this over the SAME snapshot
selection already takes (`_Snapshots.last`) — no new subscription.
"""
from datetime import date, datetime, timezone
from decimal import Decimal as D

from meic.adapters.api.server import _live_pnl_enricher
from meic.adapters.dxlink.chain_snapshot import ChainSnapshot
from meic.domain.chain import ChainSide, Mark

TAKEN_AT = datetime(2026, 7, 9, 14, 35, tzinfo=timezone.utc)


class _Snaps:
    """Minimal stand-in for server.py's `_Snapshots` holder."""

    def __init__(self, last=None):
        self.last = last


def _snapshot(put_marks: dict, call_marks: dict, *, stale: bool = False) -> ChainSnapshot:
    return ChainSnapshot(
        spot=D("7540"), expiration=date(2026, 7, 9),
        put_side=ChainSide(strikes_toward_otm=tuple(sorted(put_marks, reverse=True)), marks=put_marks),
        call_side=ChainSide(strikes_toward_otm=tuple(sorted(call_marks)), marks=call_marks),
        put_band=(), call_band=(), symbols={},
        taken_at=TAKEN_AT, stale=stale)


def _leg(side, role, strike, price, qty=1):
    return {"side": side, "role": role, "strike": strike, "price": price, "qty": qty}


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
