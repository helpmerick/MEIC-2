"""Hand-written step definitions for TC-LEX-10 — EC-LEX-08 (v1.63): the
intrinsic-floor resting order for a stopped side whose long has NO bid at
all but a fresh underlying mark makes the LEX-04 floor computable; a usable
quote arriving later supersedes the floor via the raced-fill-guarded
cancel/replace (LEX-08); with neither a bid nor a fresh mark, the side
defers honestly (a price is never invented).

Reuses tests/application/test_stop_fill_watch.py's harness (`_FakeBroker`,
`_RecoverSpy`, `_Alerts`, `_Comp`) for the simple no-price-ever-invented
scenario, and drives the REAL `RecoverLong` against the live-shaped
`tests/harness/live_broker.py` `LiveShapedBroker` for the two scenarios that
actually place/replace broker orders — the same live-shape discipline every
other LEX/STP-08a suite in this repo uses (test_stop_fill_watch.py's own
SDK-shapes test, test_tc_stp_20.py).
"""
import asyncio
from datetime import datetime
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then

from meic.application.recover_long import Quote, RecoverLong
from meic.application.stop_fill_watch import (
    NoBidFloor,
    QUOTE_DEFERRAL_ALERT_TICKS,
    detect_and_recover_stop_fills,
)
from meic.domain.events import (
    CondorFilled,
    FilledLeg,
    LexOrderPlaced,
    LongSaleStarted,
    LongSold,
    ShortStopped,
    SideClosed,
)
from meic.domain.ticks import TickRung, TickTable
from tests.application.test_stop_fill_watch import _Alerts, _Comp, _FakeBroker, _RecoverSpy
from tests.harness.fake_clock import ET, FastClock
from tests.harness.live_broker import LiveShapedBroker

scenarios("../features/TC-LEX-10.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))

ENTRY_ID = "2026-07-11#5"
PUT_LONG = "SPXW  260711P07510000"          # strike 7510 -- "long P7510"
PUT_SHORT = "SPXW  260711P07530000"
CALL_SHORT, CALL_LONG = "SPXW  260711C07570000", "SPXW  260711C07590000"


class _ReplaceSpyBroker(LiveShapedBroker):
    """Records every `replace()` call (oid, intent) so a test can assert the
    guarded cancel/replace actually ran against the ADOPTED floor order id,
    never a blind fresh submit."""

    def __init__(self, clock):
        super().__init__(clock)
        self.replace_calls: list[tuple[str, object]] = []

    async def replace(self, oid, intent):
        self.replace_calls.append((str(oid), intent))
        return await super().replace(oid, intent)


def _pending_put_world():
    """A stopped PUT side (short already stopped, long P7510 still held) --
    the EC-LEX-08 candidates all start here."""
    legs = (
        FilledLeg(symbol=PUT_LONG, right="P", role="long", qty=1, price=D("0.10")),
        FilledLeg(symbol=PUT_SHORT, right="P", role="short", qty=1, price=D("2.00")),
        FilledLeg(symbol=CALL_SHORT, right="C", role="short", qty=1, price=D("2.00")),
        FilledLeg(symbol=CALL_LONG, right="C", role="long", qty=1, price=D("0.08")),
    )
    events = [CondorFilled(entry_id=ENTRY_ID, net_credit=D("3.82"), legs=legs)]
    events.append(ShortStopped(entry_id=ENTRY_ID, side="PUT", fill=D("4.00"), slippage=D("1.50")))
    return events


async def _no_quote(symbol, side):
    return None


@pytest.fixture
def world():
    return {}


# --- Scenario: An intrinsic floor rests when the book is empty but spot is fresh ---

@given("a long P7510 with no bid, SPX at 7480, and lex_quote_wait_seconds elapsed")
def _(world):
    events = _pending_put_world()
    clock = FastClock(datetime(2026, 7, 11, 12, 0, tzinfo=ET))
    broker = LiveShapedBroker(clock)
    broker.set_positions([(PUT_LONG, 1, "Long")])
    recover = RecoverLong(broker, clock, events, SPX)
    alerts = _Alerts()
    comp = _Comp(broker, events, recover)

    async def _no_bid_floor(symbol, side):
        return NoBidFloor(intrinsic=D("30.00"))   # intrinsic_put(7510, 7480)

    asyncio.run(detect_and_recover_stop_fills(comp, alerts, _no_bid_floor))
    world.update(events=events, broker=broker, comp=comp, alerts=alerts,
                 recover=recover, no_bid_floor=_no_bid_floor)


@then("a limit sell rests at 30.00 (intrinsic floored to tick)")
def _(world):
    placed = [e for e in world["events"] if isinstance(e, LexOrderPlaced) and e.kind == "floor"]
    assert len(placed) == 1, "exactly one floor order journaled at placement"
    assert placed[0].price == D("30.00")
    assert placed[0].entry_id == ENTRY_ID and placed[0].side == "PUT"

    oid = placed[0].broker_order_id
    rec = world["broker"]._orders[oid]
    assert rec["cancelled"] is False, "the floor order genuinely rests at the broker"
    assert rec["intent"].order_type == "limit" and rec["intent"].price == D("30.00")
    assert rec["intent"].legs[0].action == "sell_to_close"

    assert world["comp"]._stop_fill_floor_orders[(ENTRY_ID, "PUT")] == (oid, D("30.00"))


@then("the one-time critical alert fires when the floor order is placed")
def _(world):
    criticals = [a for a in world["alerts"].calls if a[0] == "critical"]
    assert len(criticals) == 1
    assert "EC-LEX-08" in criticals[0][1]

    # one-time: a second tick with the floor still resting (unfilled, no new
    # quote) neither re-alerts nor places a second order.
    asyncio.run(detect_and_recover_stop_fills(world["comp"], world["alerts"], world["no_bid_floor"]))
    assert len([a for a in world["alerts"].calls if a[0] == "critical"]) == 1
    placed = [e for e in world["events"] if isinstance(e, LexOrderPlaced) and e.kind == "floor"]
    assert len(placed) == 1, "the floor is never re-placed while it already rests"


# --- Scenario: Quote resumption supersedes the floor -------------------------------

@given("the resting floor order and a usable bid arriving")
def _(world):
    events = _pending_put_world()
    clock = FastClock(datetime(2026, 7, 11, 12, 0, tzinfo=ET))
    broker = _ReplaceSpyBroker(clock)
    broker.set_positions([(PUT_LONG, 1, "Long")])
    recover = RecoverLong(broker, clock, events, SPX)
    alerts = _Alerts()
    comp = _Comp(broker, events, recover)

    async def _no_bid_floor(symbol, side):
        return NoBidFloor(intrinsic=D("30.00"))

    asyncio.run(detect_and_recover_stop_fills(comp, alerts, _no_bid_floor))
    floor_oid, floor_price = comp._stop_fill_floor_orders[(ENTRY_ID, "PUT")]

    fresh = (Quote(bid=D("31.00"), ask=D("31.20")), D("30.00"))

    async def _usable(symbol, side):
        return fresh

    asyncio.run(detect_and_recover_stop_fills(comp, alerts, _usable))

    world.update(events=events, broker=broker, comp=comp, alerts=alerts,
                 floor_oid=floor_oid, floor_price=floor_price, fresh=fresh)


@then("the raced-fill-guarded cancel/replace resumes normal ladder pricing")
def _(world):
    # The guarded cancel/replace ran, adopting the RESTING FLOOR order id --
    # never a blind fresh submit beside it.
    replaced_ids = [oid for oid, _ in world["broker"].replace_calls]
    assert world["floor_oid"] in replaced_ids

    # Normal LEX-03 pricing resumed: the new rung is priced off the FRESH
    # quote's mid, not the stale intrinsic floor price.
    rungs = [e for e in world["events"] if isinstance(e, LexOrderPlaced) and e.kind == "ladder"]
    assert len(rungs) == 1
    fresh_quote = world["fresh"][0]
    assert rungs[0].price == fresh_quote.mid
    assert rungs[0].price != world["floor_price"]
    assert rungs[0].entry_id == ENTRY_ID and rungs[0].side == "PUT"

    # RPT-07: LongSaleStarted stamps the REAL quote at ladder (re)start.
    starts = [e for e in world["events"] if isinstance(e, LongSaleStarted) and e.side == "PUT"]
    assert len(starts) == 1
    assert starts[0].mark_bid == fresh_quote.bid and starts[0].mark_ask == fresh_quote.ask

    # recover() now owns/terminates the adopted order -- the floor registry
    # entry is popped so a later tick's double-ladder guard covers it instead.
    assert (ENTRY_ID, "PUT") not in world["comp"]._stop_fill_floor_orders


def test_supersession_race_fill_during_replace_terminates_via_the_guard_never_double_sells():
    """LEX-08 mechanics proof (2026-07-10 incident-#2 class), driven against
    the live-shaped broker per the race-guard convention: if the resting
    floor's fill lands exactly inside recover()'s cancel/replace round trip,
    the pre-replace `_filled` check misses it (still resting a beat before),
    `replace()` itself raises (the broker's real margin_check_failed
    behaviour once the fill has landed), and the exception handler's
    re-check recognises the fill and terminates the side via `_sold` at the
    FLOOR price -- never a fresh rung placed beside an already-filled order."""
    events = _pending_put_world()
    clock = FastClock(datetime(2026, 7, 11, 12, 0, tzinfo=ET))
    broker = LiveShapedBroker(clock)
    broker.set_positions([(PUT_LONG, 1, "Long")])
    recover = RecoverLong(broker, clock, events, SPX)
    alerts = _Alerts()
    comp = _Comp(broker, events, recover)

    async def _no_bid_floor(symbol, side):
        return NoBidFloor(intrinsic=D("30.00"))

    asyncio.run(detect_and_recover_stop_fills(comp, alerts, _no_bid_floor))
    floor_oid, floor_price = comp._stop_fill_floor_orders[(ENTRY_ID, "PUT")]
    broker.race_fill_on_replace(floor_oid)

    fresh = (Quote(bid=D("31.00"), ask=D("31.20")), D("30.00"))

    async def _usable(symbol, side):
        return fresh

    asyncio.run(detect_and_recover_stop_fills(comp, alerts, _usable))

    sold = [e for e in events if isinstance(e, LongSold) and e.side == "PUT"]
    assert len(sold) == 1 and sold[0].recovery == floor_price, \
        "the race is caught by the post-replace re-check and terminates at the floor price"
    closed = [e for e in events if isinstance(e, SideClosed) and e.side == "PUT"]
    assert len(closed) == 1
    rungs = [e for e in events if isinstance(e, LexOrderPlaced) and e.kind == "ladder"]
    assert rungs == [], "no fresh ladder rung is ever journaled beside the raced fill"
    assert (ENTRY_ID, "PUT") not in comp._stop_fill_floor_orders


# --- Scenario: No bid and no spot defers honestly -----------------------------------

@given("neither a bid nor a fresh underlying mark")
def _(world):
    events = _pending_put_world()
    broker = _FakeBroker()
    broker.positions_ = [{"symbol": PUT_LONG, "signed_qty": 1}]
    recover, alerts = _RecoverSpy(), _Alerts()
    comp = _Comp(broker, events, recover)

    for _ in range(QUOTE_DEFERRAL_ALERT_TICKS):
        asyncio.run(detect_and_recover_stop_fills(comp, alerts, _no_quote))

    world.update(events=events, broker=broker, comp=comp, alerts=alerts, recover=recover)


@then("the side defers with the one-time critical alert and no price is ever invented")
def _(world):
    assert world["recover"].calls == [], "no price is ever invented -- no order is ever placed"
    assert not getattr(world["comp"], "_stop_fill_floor_orders", {}), \
        "no floor is placeable without a fresh underlying mark"
    criticals = [a for a in world["alerts"].calls if a[0] == "critical"]
    assert len(criticals) == 1, "the one-time critical alert stands, never re-fired per tick"

    # keeps deferring indefinitely -- one more tick, still no order, no re-alert.
    asyncio.run(detect_and_recover_stop_fills(world["comp"], world["alerts"], _no_quote))
    assert world["recover"].calls == []
    assert len([a for a in world["alerts"].calls if a[0] == "critical"]) == 1
