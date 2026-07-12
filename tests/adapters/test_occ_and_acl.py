"""OCC symbology + the TastytradeAdapter ACL (doc 05 §121: payload translation
is the adapter's job). Offline — the option lookup is injected, no session.

This is half of the test whose absence let the application emit intents the
adapter could not consume.
"""
import asyncio
import base64
import json
from datetime import date
from decimal import Decimal as D

import pytest

from meic.adapters.tastytrade.adapter import TastytradeAdapter
from meic.adapters.tastytrade.occ import occ_symbol
from meic.application.order_intent import OrderIntent, OrderLeg, condor_legs

EXP = date(2026, 7, 7)


def _jwt(iss):
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'EdDSA'})}.{seg({'iss': iss})}.sig"


CERT = _jwt("https://api.sandbox.tastyworks.com")


# --- OCC symbology, pinned to a real cert payload ------------------------------

def test_occ_symbol_matches_the_real_cert_symbol():
    assert occ_symbol("SPXW", EXP, "P", D("3000")) == "SPXW  260707P03000000"


def test_occ_symbol_pads_root_and_scales_strike_by_1000():
    assert occ_symbol("SPX", date(2026, 12, 31), "C", D("6055.5")) == "SPX   261231C06055500"
    assert len(occ_symbol("SPXW", EXP, "C", D("6060"))) == 21


def test_occ_symbol_rejects_bad_right_root_and_fractional_strike():
    with pytest.raises(ValueError, match="right"):
        occ_symbol("SPXW", EXP, "X", D("6000"))
    with pytest.raises(ValueError, match="6-char"):
        occ_symbol("TOOLONGX", EXP, "P", D("6000"))
    with pytest.raises(ValueError, match="exact thousandth"):
        occ_symbol("SPXW", EXP, "P", D("6000.0001"))


# --- the ACL: OrderIntent -> broker order, offline -----------------------------

class _FakeOption:
    """Stands in for tastytrade's Option — builds a REAL Leg so NewOrder validates,
    without a session or network."""
    def __init__(self, symbol):
        self.symbol = symbol

    def build_leg(self, qty, action):
        from tastytrade.instruments import InstrumentType
        from tastytrade.order import Leg
        return Leg(instrument_type=InstrumentType.EQUITY_OPTION, symbol=self.symbol,
                   quantity=qty, action=action)


def _adapter():
    a = TastytradeAdapter("secret", CERT, is_test=True)
    a._option_for = lambda symbol: _resolved(_FakeOption(symbol))  # inject, no network
    return a


async def _resolved(v):
    return v


def _build(intent):
    return asyncio.run(_adapter()._build_order(intent))


def test_acl_resolves_condor_strikes_to_occ_symbols_and_sizes_every_leg():
    """The original showstopper: an entry intent must translate. Four legs, real
    OCC symbols, every leg at the entry size."""
    contracts = 2
    intent = OrderIntent(
        order_type="limit", tif="Day", contracts=contracts, kind="iron_condor",
        underlying="SPXW", expiration=EXP, price=D("4.00"), entry_id="d#1",
        legs=condor_legs(put_short=D("5990"), put_long=D("5940"),
                         call_short=D("6060"), call_long=D("6110"), contracts=contracts))

    order = _build(intent)

    assert len(order.legs) == 4
    assert [l.symbol for l in order.legs] == [
        "SPXW  260707P05940000",   # long put
        "SPXW  260707P05990000",   # short put
        "SPXW  260707C06060000",   # short call
        "SPXW  260707C06110000",   # long call
    ]
    assert all(l.quantity == D(contracts) for l in order.legs)   # qty == contracts, every leg
    assert order.price == D("4.00")
    assert order.stop_trigger is None


def test_acl_translates_a_stop_and_never_sizes_it_below_the_position():
    contracts = 2
    intent = OrderIntent(
        order_type="stop_market", tif="Day", contracts=contracts, kind="stop",
        underlying="SPXW", expiration=EXP, stop_trigger=D("3.80"), entry_id="d#1",
        legs=(OrderLeg(right="P", action="buy_to_close", qty=contracts, strike=D("5990")),))

    order = _build(intent)

    assert len(order.legs) == 1
    assert order.legs[0].symbol == "SPXW  260707P05990000"
    assert order.legs[0].quantity == D(2)          # stop qty == short qty
    assert order.stop_trigger == D("3.80")
    assert order.price is None


def test_acl_passes_through_already_resolved_symbols():
    """Close/LEX legs already carry a symbol (from the OWN ledger) — no strike."""
    intent = OrderIntent(
        order_type="limit", tif="Day", contracts=1, kind="close", price=D("0.05"),
        legs=(OrderLeg(right="P", action="buy_to_close", qty=1, symbol="SPXW  260707P05990000"),))
    order = _build(intent)
    assert order.legs[0].symbol == "SPXW  260707P05990000"


def test_acl_maps_every_order_type():
    from tastytrade.order import OrderType
    put = lambda: OrderLeg(right="P", action="buy_to_close", qty=1, symbol="S")
    cases = {
        "limit": dict(price=D("1.00")),
        "marketable_limit": dict(price=D("1.00")),
        "stop_market": dict(stop_trigger=D("3.80")),
        "stop_limit": dict(stop_trigger=D("3.80"), price=D("3.90")),
    }
    expected = {"limit": OrderType.LIMIT, "marketable_limit": OrderType.MARKETABLE_LIMIT,
                "stop_market": OrderType.STOP, "stop_limit": OrderType.STOP_LIMIT}
    for otype, extra in cases.items():
        order = _build(OrderIntent(order_type=otype, tif="Day", contracts=1,
                                   legs=(put(),), **extra))
        assert order.order_type == expected[otype], otype
