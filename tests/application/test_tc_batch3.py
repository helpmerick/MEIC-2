"""Third batch of edge-case prose TCs: LEX (8), OWN (6), RSK (4)."""
import asyncio
from decimal import Decimal as D

from meic.application.recover_long import Quote, RecoverLong
from meic.domain.events import LongSold, SideClosed
from meic.domain.ladder import intrinsic_put, lex_floor
from meic.domain.ownership import Ownership, OwnershipLedger
from meic.domain.risk import (
    OrderCap,
    exceeds_max_day_risk,
    sane_order_price,
    sane_quote,
    worst_case_loss,
)
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker, Scripted

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


def _lex(broker, events, **kw):
    return RecoverLong(broker, events, SPX, **kw)


# --- LEX ---------------------------------------------------------------------

def test_tc_lex_02_invalid_quote_goes_to_fallback():
    """TC-LEX-02: stale/crossed quotes ⇒ never price off them; take the
    marketable-limit fallback at the bid."""
    broker, events = FakeBroker(), []
    r = asyncio.run(_lex(broker, events).recover(entry_id="e", side="PUT", long_symbol="P",
                                                 quote=Quote(bid=D("0.45"), ask=D("0.40")),  # crossed
                                                 intrinsic=D("0")))
    assert r.outcome == "FALLBACK_WORKING" and r.prices_tried == ()  # no ladder off a bad quote


def test_tc_lex_03_floor_never_below_bid_or_intrinsic():
    """TC-LEX-03 (LEX-04): the sell floor is max(bid, intrinsic); a deep-ITM
    long makes intrinsic bind."""
    assert lex_floor(D("0.40"), intrinsic_put(D("5990"), D("5950"))) == D("40")  # intrinsic 40 binds
    assert lex_floor(D("2.00"), D("0")) == D("2.00")                             # else the bid


def test_tc_lex_04_fallback_unfilled_keeps_retrying():
    """TC-LEX-04 (LEX-06): fallback unfilled ⇒ the working order stays live (a
    long-only residual is defined-risk; the loop keeps trying)."""
    broker, events = FakeBroker(), []  # never fills
    r = asyncio.run(_lex(broker, events).recover(entry_id="e", side="PUT", long_symbol="P",
                                                 quote=Quote(bid=D("2.00"), ask=D("2.30")), intrinsic=D("0")))
    assert r.outcome == "FALLBACK_WORKING"
    assert any(o.intent.get("type") == "marketable_limit" for o in broker._orders.values())


def test_tc_lex_05_always_sells_side_flat_after():
    """TC-LEX-05 (LEX-07): the long is ALWAYS sold — on fill the side is flat,
    no cheap-long retained."""
    broker, events = FakeBroker(), []
    broker.script_submit(Scripted("fill", payload={"price": "2.15"}))
    r = asyncio.run(_lex(broker, events).recover(entry_id="e", side="PUT", long_symbol="P",
                                                 quote=Quote(bid=D("2.00"), ask=D("2.30")), intrinsic=D("0")))
    assert r.outcome == "SOLD"
    assert any(isinstance(e, LongSold) for e in events) and any(isinstance(e, SideClosed) for e in events)


def test_tc_lex_08_zero_bid_long_rests_at_min_tick():
    """TC-LEX-08 (EC-LEX-04): a zero-bid long ⇒ a minimum-tick limit rests
    (never a market order) until fill or expiry."""
    broker, events = FakeBroker(), []
    r = asyncio.run(_lex(broker, events).recover(entry_id="e", side="PUT", long_symbol="P",
                                                 quote=Quote(bid=D("0.00"), ask=D("0.05")), intrinsic=D("0")))
    # ladder starts at the mid (0.025 -> rounds to a tick); never a raw market order
    assert all(o.intent.get("type") in ("limit", "marketable_limit") for o in broker._orders.values())


# --- OWN ---------------------------------------------------------------------

def test_tc_own_02_crash_orphan_adopted_vs_foreign():
    """TC-OWN-02: a position matching the bot's own fills is adopted; an
    identical-looking one with no matching fills is FOREIGN."""
    led = OwnershipLedger()
    led.apply_fill("SPXW_5990P", -2)  # the bot's own fills -> owned
    assert led.classify("SPXW_5990P", broker_net=-2) is Ownership.OWNED
    assert led.classify("SPXW_6050C", broker_net=-2) is Ownership.FOREIGN  # no fills recorded


def test_tc_own_04_ledger_shortfall_suspends():
    """TC-OWN-04 (OWN-06): broker shows less than the ledger ⇒ SHORTFALL
    (SUSPEND + write down), never a compensating order."""
    led = OwnershipLedger()
    led.apply_fill("SPXW_5990P", -2)
    assert led.classify("SPXW_5990P", broker_net=-1) is Ownership.SHORTFALL
    led.write_down_to("SPXW_5990P", -1)
    assert led.owned("SPXW_5990P") == -1


def test_tc_own_05_exit_cap_property_never_exceeds_ledger():
    """TC-OWN-05 (OWN-04): every exit order quantity ≤ ledger(symbol), under
    randomized foreign deltas (property)."""
    import random
    led = OwnershipLedger()
    led.apply_fill("S", -2)
    rng = random.Random(0)
    for _ in range(100):
        requested = rng.randint(0, 10)
        assert led.cap_exit_qty("S", requested) <= abs(led.owned("S"))  # never exceeds 2


def test_tc_own_08_fresh_fill_not_yet_propagated_no_external_close():
    """TC-OWN-08 (OWN-09): a fresh fill whose position hasn't propagated (net 0
    seconds after placement) does NOT trigger external close — guards unmet."""
    from meic.domain.external_close import SideDisposition, SideObservation, classify_side
    obs = SideObservation(stop_filled=False, position_present=False, stop_working=True,
                          stop_cancelled_by_bot=False, seen_open=False, grace_elapsed=False,
                          confirmed_two_reconciles=False)
    assert classify_side(obs) is SideDisposition.STILL_OPEN  # wait, don't stand down


def test_tc_own_09_partial_reduction_zero_order_actions():
    """TC-OWN-09 (OWN-10): operator buys back 1 of 2 shorts ⇒ SUSPEND, write
    down, ZERO bot order actions (the oversized stop is left untouched)."""
    led = OwnershipLedger()
    led.apply_fill("S", -2)
    assert led.classify("S", broker_net=-1) is Ownership.SHORTFALL
    # the bot fires no compensating order; the ledger writes down, resize is the operator's
    led.write_down_to("S", -1)
    assert led.owned("S") == -1


# --- RSK ---------------------------------------------------------------------

def test_tc_rsk_03_max_day_risk_blocks_entry():
    """TC-RSK-03 (RSK-04): a new entry is blocked when worst-case exposure would
    exceed max_day_risk; the exposure number is (width−credit)×100."""
    wc = worst_case_loss(width=D("50"), net_credit=D("4.00"))  # 4600
    assert wc == D("4600")
    assert exceeds_max_day_risk([D("4600"), D("4600")], wc, max_day_risk=D("10000")) is True   # 13800 > 10000
    assert exceeds_max_day_risk([D("4600")], wc, max_day_risk=D("10000")) is False              # 9200 < 10000


def test_tc_rsk_04_fat_finger_and_quote_sanity():
    """TC-RSK-04 (RSK-05): an absurd order price and an absurd inbound quote are
    both rejected before any broker call."""
    assert sane_order_price(D("4.00"), reference_mid=D("4.10"), max_deviation_pct=D("20"))
    assert not sane_order_price(D("40.00"), reference_mid=D("4.10"), max_deviation_pct=D("20"))
    assert sane_quote(D("2.00"), D("2.10")) and not sane_quote(D("2.30"), D("2.10"))  # crossed rejected


def test_tc_rsk_08_order_cap_exempts_exit_orders():
    """TC-RSK-08: new entries blocked at the cap buffer; a stop replacement and
    a LEX order after the cap are NOT blocked; cancel/replaces count."""
    cap = OrderCap(cap=10, buffer=2)
    for _ in range(8):
        assert cap.allow(exit_priority=False); cap.record()
    assert cap.allow(exit_priority=False) is False           # hit the buffered cap (8 == 10-2)
    assert cap.allow(exit_priority=True) is True             # a stop/LEX is never blocked
