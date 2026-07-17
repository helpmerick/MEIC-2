"""ORD-04/EC-API-03 (2026-07-17 security review finding A, ROOT FIX) — the
TastytradeAdapter stamps the intent's idempotency key onto the broker's
server-side `external_identifier` and resolves a lost-response-after-commit
submit by matching THAT id, never a leg-shape guess.

Verified against the installed SDK's models: `NewOrder` accepts
`external_identifier` and `PlacedOrder` returns it (external_identifier exists
in both regardless of the exact point version). These tests are the
discriminating-power coverage the review said was missing (findings 2/3/4/F1):
  * `_build_order` stamps the key.
  * `find_matching_order` adopts the order carrying OUR id.
  * a DECOY order (different external id, identical legs) is NOT adopted --
    structural matching could not tell it apart; external_identifier can.
  * a CANCELLED/REJECTED/EXPIRED order carrying our id is NOT adopted.
  * a still-transient Routed / In Flight order carrying our id IS adopted (F1).
  * a keyless intent claims nothing.
"""
import asyncio
import base64
import json
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from meic.adapters.tastytrade.adapter import TastytradeAdapter
from meic.application.order_intent import OrderIntent, condor_legs

EXP = date(2026, 7, 17)


def _jwt(iss):
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'EdDSA'})}.{seg({'iss': iss})}.sig"


CERT = _jwt("https://api.sandbox.tastyworks.com")


class _FakeOption:
    """Builds a REAL SDK Leg so NewOrder validates, without a session."""
    def __init__(self, symbol):
        self.symbol = symbol

    def build_leg(self, qty, action):
        from tastytrade.instruments import InstrumentType
        from tastytrade.order import Leg
        return Leg(instrument_type=InstrumentType.EQUITY_OPTION, symbol=self.symbol,
                   quantity=qty, action=action)


def _adapter():
    a = TastytradeAdapter("secret", CERT, is_test=True)
    a._option_for = lambda symbol: _resolved(_FakeOption(symbol))
    return a


async def _resolved(v):
    return v


def _entry_intent(entry_id="d#1"):
    return OrderIntent(
        order_type="limit", tif="Day", contracts=1, kind="iron_condor",
        underlying="SPXW", expiration=EXP, price=D("4.00"), entry_id=entry_id,
        idempotency_key=f"entry:{entry_id}",
        legs=condor_legs(put_short=D("5990"), put_long=D("5940"),
                         call_short=D("6060"), call_long=D("6110"), contracts=1))


# --- _build_order stamps the id ------------------------------------------------

def test_build_order_stamps_external_identifier_from_idempotency_key():
    order = asyncio.run(_adapter()._build_order(_entry_intent("d#1")))
    assert order.external_identifier == "entry:d#1"


def test_build_order_leaves_external_identifier_unset_for_a_keyless_intent():
    from meic.application.order_intent import OrderLeg
    keyless = OrderIntent(order_type="limit", tif="Day", contracts=1, price=D("1.00"),
                          legs=(OrderLeg(right="P", action="buy_to_close", qty=1, symbol="S"),))
    order = asyncio.run(_adapter()._build_order(keyless))
    assert order.external_identifier is None


# --- find_matching_order: SDK-shaped live orders, injected ---------------------

def _placed(external_identifier, status, id):
    """A PlacedOrder-shaped stand-in: attribute access only, no `.get`."""
    return SimpleNamespace(external_identifier=external_identifier, status=status, id=id)


def _adapter_with_live(orders):
    a = TastytradeAdapter("secret", CERT, is_test=True)

    class _Account:
        async def get_live_orders(self, session):
            return list(orders)

    a._account = _Account()
    return a


def test_find_matching_order_adopts_the_order_carrying_our_stamped_id():
    a = _adapter_with_live([_placed("entry:d#1", "Live", 482314017)])
    got = asyncio.run(a.find_matching_order(_entry_intent("d#1")))
    assert got == "482314017"


def test_find_matching_order_ignores_a_decoy_with_identical_legs_but_a_different_id():
    """FINDING 4 (discriminating power): the operator (or a prior entry) holds
    a structurally IDENTICAL order (same strikes/expiry/legs) under a DIFFERENT
    external id. A leg-shape match would adopt it -- reopening OWN-01/OWN-03 on
    a shared account. The stamped-id match must reject it and adopt only OUR
    order."""
    a = _adapter_with_live([
        _placed("entry:SOMEONE-ELSE", "Live", 999_000),   # the decoy: same legs, foreign id
        _placed("entry:d#1", "Live", 482314017),          # ours
    ])
    got = asyncio.run(a.find_matching_order(_entry_intent("d#1")))
    assert got == "482314017"   # never the decoy 999000


def test_find_matching_order_never_adopts_a_cancelled_order_with_our_id():
    """FINDING 2/3: a prior unfilled_at_floor entry left a CANCELLED order
    reusing `entry:d#1`. It carries our id but is terminal -- adopting it as
    live would treat a dead order as a resting position. The status filter
    must exclude it, so the query reports 'not found' (submit truly failed)."""
    a = _adapter_with_live([_placed("entry:d#1", "Cancelled", 482314017)])
    assert asyncio.run(a.find_matching_order(_entry_intent("d#1"))) is None


def test_find_matching_order_never_adopts_a_rejected_order_with_our_id():
    a = _adapter_with_live([_placed("entry:d#1", "Rejected", 482314017)])
    assert asyncio.run(a.find_matching_order(_entry_intent("d#1"))) is None


def test_find_matching_order_never_adopts_an_expired_order_with_our_id():
    """FINDING 2/3: Expired is terminal-dead too -- never adopted."""
    a = _adapter_with_live([_placed("entry:d#1", "Expired", 482314017)])
    assert asyncio.run(a.find_matching_order(_entry_intent("d#1"))) is None


def test_find_matching_order_never_adopts_a_removed_order_with_our_id():
    """FINDING 2/3: Removed / Partially Removed are terminal-dead -- the
    normalizer folds the enum's underscore form to the space form so both map."""
    a = _adapter_with_live([_placed("entry:d#1", "OrderStatus.PARTIALLY_REMOVED", 42)])
    assert asyncio.run(a.find_matching_order(_entry_intent("d#1"))) is None


# --- FINDING F1: a just-placed order transits Routed/In Flight before Live -----
# and MUST be adopted mid-transit, or missing it re-raises into the false
# "no position taken" skip -- the exact naked, stopless condor this fix closes.
# (Direction proven by the SDK's own OrderStatus enum and the real placed-order
# fixture; the asymmetry: adopting a transient that dies is safe, missing a
# live one is catastrophic.)

def test_find_matching_order_adopts_a_routed_order_with_our_id():
    """F1: Routed is a transient placement state, NOT terminal -- adopt it."""
    a = _adapter_with_live([_placed("entry:d#1", "Routed", 482314017)])
    assert asyncio.run(a.find_matching_order(_entry_intent("d#1"))) == "482314017"


def test_find_matching_order_adopts_an_in_flight_order_with_our_id():
    """F1: In Flight (a two-word status) is transient -- adopt it. Pins the
    normalizer's space/underscore handling against both wire forms."""
    a = _adapter_with_live([_placed("entry:d#1", "In Flight", 482314017)])
    assert asyncio.run(a.find_matching_order(_entry_intent("d#1"))) == "482314017"
    # the enum-repr form normalizes identically
    b = _adapter_with_live([_placed("entry:d#1", "OrderStatus.IN_FLIGHT", 482314017)])
    assert asyncio.run(b.find_matching_order(_entry_intent("d#1"))) == "482314017"


def test_find_matching_order_adopts_a_contingent_order_with_our_id():
    """F1: Contingent is a live-pending state, NOT terminal -- adopt it."""
    a = _adapter_with_live([_placed("entry:d#1", "Contingent", 482314017)])
    assert asyncio.run(a.find_matching_order(_entry_intent("d#1"))) == "482314017"


def test_find_matching_order_adopts_a_filled_order_carrying_our_id():
    """The lost-ack-after-fill case: the order not only landed, it filled
    before the ack was lost. A Filled status carrying our id is adoptable."""
    a = _adapter_with_live([_placed("entry:d#1", "Filled", 482314017)])
    assert asyncio.run(a.find_matching_order(_entry_intent("d#1"))) == "482314017"


def test_find_matching_order_returns_none_when_no_live_order_carries_our_id():
    a = _adapter_with_live([_placed("entry:OTHER", "Live", 1), _placed(None, "Live", 2)])
    assert asyncio.run(a.find_matching_order(_entry_intent("d#1"))) is None


def test_find_matching_order_refuses_to_claim_ownership_for_a_keyless_intent():
    """No client id => no ownership claim. Even if a live order with a null
    external id exists, a keyless intent adopts nothing."""
    from meic.application.order_intent import OrderLeg
    keyless = OrderIntent(order_type="limit", tif="Day", contracts=1, price=D("1.00"),
                          legs=(OrderLeg(right="P", action="buy_to_close", qty=1, symbol="S"),))
    a = _adapter_with_live([_placed(None, "Live", 1), _placed("", "Live", 2)])
    assert asyncio.run(a.find_matching_order(keyless)) is None
