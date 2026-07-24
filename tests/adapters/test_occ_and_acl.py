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
from meic.application.order_intent import (
    OrderIntent, OrderLeg, condor_legs, marketable_close,
)

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
    # ORD-01 entry condor: mixed buy_to_open/sell_to_open legs -- the ACL's
    # buy/sell price-signing rule (LEX-05/STP-08a/CLS-01) does not apply to a
    # mixed-action order; the net-credit price passes through UNCHANGED,
    # exactly as this live-proven path already sends it (regression pin).
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
    # STP-03 (v1.67 tombstone): stop_limit is deliberately absent -- it is not
    # a constructible OrderIntent.order_type at all (order_intent.py's
    # ORDER_TYPES), so there is no ACL mapping to test here any more. See
    # tests/bdd/test_tc_nfr_07_stp03_tombstone.py for the absence test.
    #
    # LEX-05/STP-08a/CLS-01 (PROD dry-run 2026-07-24): tastytrade's native
    # "Marketable Limit" wire type REJECTS a client price
    # (`order_must_omit_price`), so "marketable_limit" maps to the same plain
    # OrderType.LIMIT as "limit" -- never OrderType.MARKETABLE_LIMIT, never
    # OrderType.MARKET (a raw market order is spec-forbidden, LEX-05).
    from tastytrade.order import OrderType
    put = lambda: OrderLeg(right="P", action="buy_to_close", qty=1, symbol="S")
    cases = {
        "limit": dict(price=D("1.00")),
        "marketable_limit": dict(price=D("1.00")),
        "stop_market": dict(stop_trigger=D("3.80")),
    }
    expected = {"limit": OrderType.LIMIT, "marketable_limit": OrderType.LIMIT,
                "stop_market": OrderType.STOP}
    for otype, extra in cases.items():
        order = _build(OrderIntent(order_type=otype, tif="Day", contracts=1,
                                   legs=(put(),), **extra))
        assert order.order_type == expected[otype], otype


def test_acl_marketable_close_builds_plain_limit_with_negative_debit_price():
    """LEX-05/STP-08a/CLS-01 (PROD dry-run 2026-07-24): a `marketable_close`
    intent -- the shape TPF/TPT/CLS-01 manual closes, STP-03b watchdog
    escalation, and the LEX-05 fallback all build -- is a single BUY leg.
    tastytrade's native "Marketable Limit" wire type rejects a client price
    (`order_must_omit_price`), so the ACL must emit a plain OrderType.LIMIT
    (never MARKETABLE_LIMIT, never MARKET). And tastytrade prices are signed
    net effect: a BUY is a net DEBIT -- negative (a positive-priced BUY
    rejects `cant_buy_for_credit`, PROD dry-run 2026-07-24; a negative one is
    accepted)."""
    from tastytrade.order import OrderType
    intent = marketable_close(
        entry_id="d#1", right="P", contracts=1, price=D("0.05"),
        symbol="SPXW  260707P05990000")
    order = _build(intent)
    assert order.order_type == OrderType.LIMIT
    assert order.price == D("-0.05")


def test_acl_marketable_limit_sell_to_close_gets_plain_limit_positive_price():
    """LEX-05 fallback shape: closing a short via the bounded-marketable
    fallback is a SELL -- a net CREDIT, so the sign stays positive (the LEX
    ladder's live-proven sign, unchanged by this fix)."""
    from tastytrade.order import OrderType
    intent = OrderIntent(
        order_type="marketable_limit", tif="Day", contracts=1, kind="close",
        price=D("0.30"),
        legs=(OrderLeg(right="P", action="sell_to_close", qty=1,
                       symbol="SPXW  260707P05990000"),))
    order = _build(intent)
    assert order.order_type == OrderType.LIMIT
    assert order.price == D("0.30")


def test_acl_plain_limit_single_buy_gets_negative_price_dcy02_buyback_shape():
    """DCY-02 decay buyback (`decay_watcher.py`): order_type="limit",
    kind="decay", a single buy_to_close leg at a positive premium -- the same
    signed-debit dialect applies regardless of wire order_type (this sign bug
    was never yet exercised live per the 2026-07-24 dry-run probes).
    (buy_to_open signs identically -- both buy actions are net debits.)"""
    from tastytrade.order import OrderType
    intent = OrderIntent(
        order_type="limit", tif="Day", contracts=1, kind="decay",
        price=D("0.20"),
        legs=(OrderLeg(right="C", action="buy_to_close", qty=1,
                       symbol="SPXW  260707C06060000"),))
    order = _build(intent)
    assert order.order_type == OrderType.LIMIT
    assert order.price == D("-0.20")
