"""LEX-01 order-id journaling (v1.62, operator-ratified from the EOD-03
wiring flag): every LEX ladder order journals its broker order id AT
PLACEMENT (`LexOrderPlaced`, mirroring the DecayBuybackPlaced v1.61
precedent) — the initial rung submit, EVERY cancel/replace (a replace mints a
NEW id), and the LEX-05 marketable fallback — so LEX orders are auditable and
included in the EOD-03 day-end order sweep (see `_journaled_own_order_ids` in
server.py, exercised in tests/application/test_live_app.py).

Also pins the STP-08a (v1.62) `quote_stale` routing on RecoverLong itself: a
stale-invalid quote (LEX-02's age criterion, judged upstream) must never
price a LADDER — straight to the LEX-05 marketable-at-bid fallback.
"""
import asyncio
from datetime import datetime
from decimal import Decimal as D

from meic.application.recover_long import Quote, RecoverLong
from meic.domain.events import LexOrderPlaced, LongSaleRepriced, LongSaleStarted
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import ET, FastClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
SCHEDULED = datetime(2026, 7, 11, 12, 0, tzinfo=ET)


def _rec(events, broker):
    # FastClock: RecoverLong's reprice-gap waits jump straight to the deadline
    # (same choice as test_recover_long_mark_at_stop.py).
    return RecoverLong(broker, FastClock(SCHEDULED), events, SPX)


def test_lex_ids_journaled_on_submit_every_replace_and_the_exhausted_fallback():
    """LEX-01 (v1.62): the ladder rests unfilled through every rung (FakeBroker
    default = WORK), so the full order lifecycle runs — initial submit, three
    replaces (mid 2.15 stepping 0.05 to the 2.00 bid floor, LEX-04), then the
    LEX-05 fallback. FIVE broker ids are minted; every one must be journaled
    at placement, in placement order, with the right kind."""
    events: list = []
    broker = FakeBroker()
    rec = _rec(events, broker)

    outcome = asyncio.run(rec.recover(
        entry_id="e1", side="PUT", long_symbol="SPXW_5940P",
        quote=Quote(bid=D("2.00"), ask=D("2.30")), intrinsic=D("0")))

    assert outcome.outcome == "FALLBACK_WORKING"
    placed = [e for e in events if isinstance(e, LexOrderPlaced)]
    rungs = [e for e in placed if e.kind == "ladder"]
    fallbacks = [e for e in placed if e.kind == "fallback"]

    # every rung the ladder actually priced journals its id — one per
    # LongSaleRepriced, same order, id minted fresh per submit/replace
    repriced = [e for e in events if isinstance(e, LongSaleRepriced)]
    assert len(rungs) == len(repriced) == 4
    assert [e.price for e in rungs] == [e.price for e in repriced] \
        == [D("2.15"), D("2.10"), D("2.05"), D("2.00")]
    assert [e.broker_order_id for e in rungs] == ["FB-1", "FB-2", "FB-3", "FB-4"], \
        "a replace mints a NEW broker id and each one must be journaled"

    # the LEX-05 fallback is a LEX order like any rung: id journaled too
    assert len(fallbacks) == 1
    assert fallbacks[0].broker_order_id == "FB-5"
    assert fallbacks[0].price == D("2.00")   # marketable limit at the bid

    # nothing unjournaled: every id the broker ever minted is on the log
    assert {e.broker_order_id for e in placed} == set(broker._orders.keys())
    assert all(e.entry_id == "e1" and e.side == "PUT" for e in placed)


def test_lex_id_journaled_before_any_fill_can_be_seen():
    """Journal-at-placement, not at terminal: a rung that fills IMMEDIATELY
    (scripted fill on the first submit) still leaves its id on the log —
    exactly one ladder id, no fallback."""
    from tests.harness.fake_broker import Scripted

    events: list = []
    broker = FakeBroker()
    broker.script_submit(Scripted("fill", payload={"price": D("2.15")}))
    rec = _rec(events, broker)

    outcome = asyncio.run(rec.recover(
        entry_id="e1", side="CALL", long_symbol="SPXW_5960C",
        quote=Quote(bid=D("2.00"), ask=D("2.30")), intrinsic=D("0")))

    assert outcome.outcome == "SOLD"
    placed = [e for e in events if isinstance(e, LexOrderPlaced)]
    assert len(placed) == 1 and placed[0].kind == "ladder"
    assert placed[0].broker_order_id == "FB-1"


def test_invalid_quote_fallback_journals_the_fallback_id():
    """LEX-02 crossed quote ⇒ straight to the LEX-05 fallback: the ONLY order
    created is the marketable fallback, and its id is journaled."""
    events: list = []
    broker = FakeBroker()
    rec = _rec(events, broker)

    outcome = asyncio.run(rec.recover(
        entry_id="e1", side="CALL", long_symbol="SPXW_5960C",
        quote=Quote(bid=D("2.30"), ask=D("2.00")),  # crossed — LEX-02 invalid
        intrinsic=D("0")))

    assert outcome.outcome == "FALLBACK_WORKING"
    placed = [e for e in events if isinstance(e, LexOrderPlaced)]
    assert [(e.kind, e.broker_order_id, e.price) for e in placed] \
        == [("fallback", "FB-1", D("2.30"))]


def test_quote_stale_routes_straight_to_the_fallback_never_a_ladder():
    """STP-08a (v1.62): `quote_stale=True` is LEX-02's age criterion judged
    upstream (DAT-02 snapshot staleness). The quote LOOKS usable (not crossed,
    tight spread) but a ladder must never price off stale marks — one
    marketable fallback at the bid, zero rungs; LongSaleStarted still stamps
    the honest best-available mark."""
    events: list = []
    broker = FakeBroker()
    rec = _rec(events, broker)

    outcome = asyncio.run(rec.recover(
        entry_id="e1", side="PUT", long_symbol="SPXW_5940P",
        quote=Quote(bid=D("2.00"), ask=D("2.10")),  # would ladder if fresh
        intrinsic=D("0"), quote_stale=True))

    assert outcome.outcome == "FALLBACK_WORKING"
    placed = [e for e in events if isinstance(e, LexOrderPlaced)]
    assert [(e.kind, e.price) for e in placed] == [("fallback", D("2.00"))]
    assert not any(isinstance(e, LongSaleRepriced) for e in events), \
        "a stale quote must never price a ladder rung"
    starts = [e for e in events if isinstance(e, LongSaleStarted)]
    assert len(starts) == 1 and starts[0].mark_bid == D("2.00")
