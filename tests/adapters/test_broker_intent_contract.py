"""THE broker intent contract — one schema, every implementation.

Operator ruling (v1.44): "The intent-contract suite runs against EVERY broker
implementation — TastytradeAdapter, SimulatedBroker, FakeBroker — one schema,
all consumers proven against it in CI. The dialect fork must be structurally
unrepeatable, not just fixed."

What went wrong before this file existed: the application emitted one dict
dialect (`{"type", "net_credit", "legs": 4, "leg": "short_put", "trigger"}`),
SimulatedBroker read exactly that dialect, and the real TastytradeAdapter read a
DIFFERENT one (`{"order_type", "price", "legs": [...], "stop_trigger"}`). 483
tests were green. Every live order submit would have crashed:

    ENTRY: TypeError: 'int' object is not iterable   <- "legs": 4
    STOP:  KeyError: 'legs'                          <- emitted "leg": "short_put"

Nothing here mocks the schema. Each broker is handed the SAME canonical intents
and must accept them. Add a fourth broker and it must pass this file too.

Rules: ORD-01 (4-leg condor), ORD-04 (idempotency), STP-01/06 (stop per short),
ENT-04 (per-entry contracts), assumption 2 (option stops are Day-TIF).
"""
import asyncio
import base64
import json
from datetime import date
from decimal import Decimal as D

import pytest

from meic.adapters.sim.simulated_broker import SimLedger, SimulatedBroker
from meic.adapters.tastytrade.adapter import TastytradeAdapter
from meic.application.order_intent import (
    OrderIntent,
    OrderLeg,
    condor_legs,
    marketable_close,
    protective_stop,
)
from tests.harness.fake_broker import FakeBroker

EXP = date(2026, 7, 7)


def _jwt(iss):
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'EdDSA'})}.{seg({'iss': iss})}.sig"


CERT = _jwt("https://api.sandbox.tastyworks.com")


# --- the canonical intents every broker must accept ----------------------------

def condor(contracts=1, price=D("4.00")):
    """ORD-01: one 4-leg iron condor, sold for a net credit."""
    return OrderIntent(
        order_type="limit", tif="Day", kind="iron_condor", entry_id="2026-07-07#1",
        contracts=contracts, price=price, underlying="SPXW", expiration=EXP,
        idempotency_key="entry:2026-07-07#1",
        legs=condor_legs(put_short=D("5990"), put_long=D("5940"),
                         call_short=D("6060"), call_long=D("6110"), contracts=contracts))


def stop(contracts=1):
    """STP-01/06: the broker-resting buy-to-close stop-market on ONE short."""
    return protective_stop(entry_id="2026-07-07#1", right="P", contracts=contracts,
                           trigger=D("3.80"), strike=D("5990"), expiration=EXP,
                           idempotency_key="stop:2026-07-07#1:PUT")


def escalation(contracts=1):
    """STP-03b: marketable buy-to-close, symbol already resolved."""
    return marketable_close(entry_id="2026-07-07#1", right="C", contracts=contracts,
                            price=D("6.00"), symbol="SPXW  260707C06060000",
                            kind="escalation", idempotency_key="escalate:x:CALL")


def lex(contracts=1):
    """LEX-01: sell the orphaned long back, symbol from the OWN ledger."""
    return OrderIntent(
        order_type="limit", tif="Day", kind="lex", entry_id="2026-07-07#1",
        contracts=contracts, price=D("0.40"), idempotency_key="lex:x:PUT",
        legs=(OrderLeg(right="P", action="sell_to_close", qty=contracts,
                       symbol="SPXW  260707P05940000"),))


CANONICAL = {"condor": condor, "stop": stop, "escalation": escalation, "lex": lex}


# --- the three implementations, behind one uniform "submit this" seam ----------

class _FakeOption:
    """Stands in for tastytrade's Option: builds a REAL Leg so NewOrder validates,
    with no session and no network."""
    def __init__(self, symbol):
        self.symbol = symbol

    def build_leg(self, qty, action):
        from tastytrade.instruments import InstrumentType
        from tastytrade.order import Leg
        return Leg(instrument_type=InstrumentType.EQUITY_OPTION, symbol=self.symbol,
                   quantity=qty, action=action)


async def _resolved(v):
    return v


def _tastytrade():
    a = TastytradeAdapter("secret", CERT, is_test=True)
    a._option_for = lambda symbol: _resolved(_FakeOption(symbol))
    return a


def _accept_tastytrade(intent):
    """The adapter's acceptance IS translation: it must build a real NewOrder."""
    order = asyncio.run(_tastytrade()._build_order(intent))
    return [(l.symbol, int(l.quantity)) for l in order.legs]


def _accept_sim(intent):
    b = SimulatedBroker(SimLedger(cash=D("1000000")))
    oid = asyncio.run(b.submit(intent))
    o = b._orders[oid]
    assert o.status != "REJECTED", "sim rejected a well-funded canonical intent"
    return [(l.symbol or str(l.strike), l.qty) for l in o.intent.legs]


def _accept_fake(intent):
    b = FakeBroker()
    oid = asyncio.run(b.submit(intent))
    o = b._orders[oid]
    return [(l.symbol or str(l.strike), l.qty) for l in o.intent.legs]


BROKERS = {
    "TastytradeAdapter": _accept_tastytrade,
    "SimulatedBroker": _accept_sim,
    "FakeBroker": _accept_fake,
}


# --- the contract ---------------------------------------------------------------

@pytest.mark.parametrize("broker", BROKERS)
@pytest.mark.parametrize("intent_name", CANONICAL)
def test_every_broker_accepts_every_canonical_intent(broker, intent_name):
    """The test whose absence let paper and live speak different dialects."""
    legs = BROKERS[broker](CANONICAL[intent_name]())
    assert legs, f"{broker} produced no legs for {intent_name}"


@pytest.mark.parametrize("broker", BROKERS)
@pytest.mark.parametrize("intent_name", CANONICAL)
@pytest.mark.parametrize("contracts", [1, 2, 10])
def test_qty_equals_contracts_on_every_leg_including_stops(broker, intent_name, contracts):
    """The operator's invariant, asserted in the suite as ratified: a stop or leg
    sized below the position leaves it partially naked."""
    legs = BROKERS[broker](CANONICAL[intent_name](contracts))
    assert [qty for _, qty in legs] == [contracts] * len(legs), (
        f"{broker}/{intent_name}: leg quantities {[q for _, q in legs]} != contracts {contracts}")


@pytest.mark.parametrize("broker", BROKERS)
def test_the_stop_is_never_smaller_than_the_condor_it_protects(broker):
    """The concrete hazard, end to end: 2-contract condor, 2-contract stop."""
    condor_qty = {q for _, q in BROKERS[broker](condor(2))}
    stop_qty = {q for _, q in BROKERS[broker](stop(2))}
    assert condor_qty == {2} and stop_qty == {2}


@pytest.mark.parametrize("broker", BROKERS)
def test_the_condor_is_four_legs_in_ord01_order(broker):
    legs = BROKERS[broker](condor())
    assert len(legs) == 4  # ORD-01: never 2, never the integer 4


@pytest.mark.parametrize("broker", ["SimulatedBroker", "FakeBroker"])
def test_brokers_refuse_the_old_dict_dialect(broker):
    """The fork made structurally unrepeatable: a raw dict is a TypeError, not a
    silently-accepted second schema. (The adapter refuses it too, by AttributeError
    on the first field access — it never had a dict path.)"""
    impls = {"SimulatedBroker": SimulatedBroker(), "FakeBroker": FakeBroker()}
    with pytest.raises(TypeError, match="expects an OrderIntent"):
        asyncio.run(impls[broker].submit(
            {"type": "limit", "net_credit": "4.00", "legs": 4, "tif": "Day"}))


def test_tastytrade_resolves_strikes_to_the_real_occ_symbols():
    """The other half of the showstopper: strikes must become symbols the broker
    accepts. Pinned to the symbol observed in a real cert order payload."""
    legs = _accept_tastytrade(condor(2))
    assert [s for s, _ in legs] == [
        "SPXW  260707P05940000",   # long put   (buy_to_open)
        "SPXW  260707P05990000",   # short put  (sell_to_open)
        "SPXW  260707C06060000",   # short call (sell_to_open)
        "SPXW  260707C06110000",   # long call  (buy_to_open)
    ]


def test_sim_derives_its_own_margin_and_rejects_an_unaffordable_condor():
    """SIM-04: the paper BP gate reads the intent's strikes — no `margin_req`
    side-channel key, which would be a second source of truth."""
    b = SimulatedBroker(SimLedger(cash=D("1000")))       # (50 - 4) * 100 = 4600 needed
    oid = asyncio.run(b.submit(condor()))
    assert b._orders[oid].status == "REJECTED"
    assert b.events[-1]["reason"] == "rejected_bp"


def test_sim_margin_scales_with_per_entry_contracts():
    """RSK-04 (v1.44): 2 contracts require twice the worst case."""
    from meic.adapters.sim.simulated_broker import condor_margin
    assert condor_margin(condor(1)) == D("4600")   # (50 - 4) * 100 * 1
    assert condor_margin(condor(2)) == D("9200")   # ... * 2, never n x max
    assert condor_margin(stop(1)) is None          # only opening condors hold margin


def test_sim_never_mutates_the_frozen_intent():
    """The PAPER stamp lives on the order record. The intent is immutable — the
    same object may be handed to `replace()` on the next reprice rung."""
    b = SimulatedBroker(SimLedger(cash=D("1000000")))
    intent = condor()
    b.set_market(lambda i: (D("4.00"), D("4.00"), True))
    oid = asyncio.run(b.submit(intent))
    o = b._orders[oid]
    assert o.status == "FILLED" and o.mode == "PAPER"
    assert o.intent is intent  # not a copy, not mutated
