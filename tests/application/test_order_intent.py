"""The canonical order intent. These tests pin the invariants that make the
paper/live dialect fork — and the naked-stop hazard — structurally impossible."""
from datetime import date
from decimal import Decimal as D

import pytest

from meic.application.order_intent import (
    IntentError,
    OrderIntent,
    OrderLeg,
    condor_legs,
)

EXP = date(2026, 7, 8)


def _condor(contracts=1):
    return OrderIntent(
        order_type="limit", tif="Day", contracts=contracts, kind="iron_condor",
        expiration=EXP, price=D("4.00"), entry_id="d#1",
        legs=condor_legs(put_short=D("5990"), put_long=D("5940"),
                         call_short=D("6060"), call_long=D("6110"), contracts=contracts))


def _stop(contracts=1, qty=None):
    return OrderIntent(
        order_type="stop_market", tif="Day", contracts=contracts, kind="stop",
        expiration=EXP, stop_trigger=D("3.80"), entry_id="d#1",
        legs=(OrderLeg(right="P", action="buy_to_close",
                       qty=contracts if qty is None else qty, strike=D("5990")),))


# --- THE invariant: qty == contracts on every leg, stops included -------------

def test_a_stop_sized_below_the_position_cannot_be_constructed():
    """A 2-contract condor protected by a 1-contract stop leaves half the short
    position naked. Make it unconstructable."""
    with pytest.raises(IntentError, match="partially naked"):
        _stop(contracts=2, qty=1)


def test_every_condor_leg_carries_the_entry_size():
    for n in (1, 2, 10):
        intent = _condor(n)
        assert len(intent.legs) == 4
        assert all(leg.qty == n for leg in intent.legs)
        assert intent.contracts == n


def test_a_condor_with_one_mismatched_leg_is_refused():
    legs = condor_legs(put_short=D("5990"), put_long=D("5940"),
                       call_short=D("6060"), call_long=D("6110"), contracts=2)
    bad = legs[:3] + (OrderLeg(right="C", action="buy_to_open", qty=1, strike=D("6110")),)
    with pytest.raises(IntentError, match="leg 3 qty 1 != contracts 2"):
        OrderIntent(order_type="limit", tif="Day", contracts=2, expiration=EXP,
                    price=D("4.00"), legs=bad)


def test_stop_qty_equals_short_qty_at_every_size():
    for n in (1, 2, 10):
        assert _stop(n).legs[0].qty == n


# --- leg identity: exactly one of strike or symbol ----------------------------

def test_leg_needs_exactly_one_identification_path():
    with pytest.raises(IntentError, match="exactly one"):
        OrderLeg(right="P", action="sell_to_open", qty=1)  # neither
    with pytest.raises(IntentError, match="exactly one"):
        OrderLeg(right="P", action="sell_to_open", qty=1, strike=D("5990"), symbol="X")  # both
    assert OrderLeg(right="P", action="sell_to_open", qty=1, strike=D("5990")).symbol is None
    assert OrderLeg(right="P", action="buy_to_close", qty=1, symbol="SPXW  260708P05990000").strike is None


def test_strike_identified_legs_require_an_expiration():
    with pytest.raises(IntentError, match="require an expiration"):
        OrderIntent(order_type="limit", tif="Day", contracts=1, price=D("1.00"),
                    legs=(OrderLeg(right="P", action="sell_to_open", qty=1, strike=D("5990")),))


def test_symbol_identified_legs_need_no_expiration():
    intent = OrderIntent(order_type="limit", tif="Day", contracts=1, price=D("0.05"),
                         legs=(OrderLeg(right="P", action="buy_to_close", qty=1, symbol="SPXW_P"),))
    assert intent.expiration is None


# --- type/price/trigger coherence ---------------------------------------------

def test_stop_requires_a_trigger_and_forbids_a_price():
    with pytest.raises(IntentError, match="requires stop_trigger"):
        OrderIntent(order_type="stop_market", tif="Day", contracts=1, expiration=EXP,
                    legs=(OrderLeg(right="P", action="buy_to_close", qty=1, strike=D("5990")),))
    with pytest.raises(IntentError, match="must not carry price"):
        OrderIntent(order_type="stop_market", tif="Day", contracts=1, expiration=EXP,
                    stop_trigger=D("3.80"), price=D("1.00"),
                    legs=(OrderLeg(right="P", action="buy_to_close", qty=1, strike=D("5990")),))


def test_limit_requires_a_price_and_forbids_a_trigger():
    with pytest.raises(IntentError, match="requires price"):
        OrderIntent(order_type="limit", tif="Day", contracts=1, expiration=EXP,
                    legs=(OrderLeg(right="P", action="sell_to_open", qty=1, strike=D("5990")),))
    with pytest.raises(IntentError, match="must not carry stop_trigger"):
        OrderIntent(order_type="limit", tif="Day", contracts=1, expiration=EXP, price=D("1.00"),
                    stop_trigger=D("3.80"),
                    legs=(OrderLeg(right="P", action="sell_to_open", qty=1, strike=D("5990")),))


def test_option_stops_are_day_tif_only():
    """Assumption 2, refused at construction — never sent to a broker."""
    with pytest.raises(IntentError, match="Day-TIF"):
        OrderIntent(order_type="stop_market", tif="GTC", contracts=1, expiration=EXP,
                    stop_trigger=D("3.80"),
                    legs=(OrderLeg(right="P", action="buy_to_close", qty=1, strike=D("5990")),))


# --- basic well-formedness -----------------------------------------------------

def test_rejects_unknown_type_action_right_and_empty_legs():
    with pytest.raises(IntentError, match="order_type"):
        OrderIntent(order_type="market", tif="Day", contracts=1, price=D("1"),
                    legs=(OrderLeg(right="P", action="sell_to_open", qty=1, symbol="X"),))
    with pytest.raises(IntentError, match="at least one leg"):
        OrderIntent(order_type="limit", tif="Day", contracts=1, price=D("1"), legs=())
    with pytest.raises(IntentError, match="right"):
        OrderLeg(right="X", action="sell_to_open", qty=1, symbol="S")
    with pytest.raises(IntentError, match="action"):
        OrderLeg(right="P", action="yolo", qty=1, symbol="S")
    with pytest.raises(IntentError, match="qty must be"):
        OrderLeg(right="P", action="sell_to_open", qty=0, symbol="S")


def test_condor_leg_order_and_actions_are_ord01_canonical():
    legs = condor_legs(put_short=D("5990"), put_long=D("5940"),
                       call_short=D("6060"), call_long=D("6110"), contracts=1)
    assert [(l.right, l.action, l.strike) for l in legs] == [
        ("P", "buy_to_open", D("5940")),
        ("P", "sell_to_open", D("5990")),
        ("C", "sell_to_open", D("6060")),
        ("C", "buy_to_open", D("6110")),
    ]


def test_is_stop_flag():
    assert _stop().is_stop is True and _condor().is_stop is False
