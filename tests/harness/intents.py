"""Canonical intents for tests.

Tests used to hand-roll order dicts, which is exactly how the paper/live dialect
fork survived 483 green tests. Build test orders from here, or from
`meic.application.order_intent` directly — never from a literal dict.
"""
from datetime import date
from decimal import Decimal as D

from meic.application.order_intent import (
    OrderIntent,
    OrderLeg,
    condor_legs,
    marketable_close,
    protective_stop,
)

EXP = date(2026, 7, 7)
PUT_SHORT = "SPXW  260707P05990000"
PUT_LONG = "SPXW  260707P05940000"
CALL_SHORT = "SPXW  260707C06060000"
CALL_LONG = "SPXW  260707C06110000"


def condor_intent(price="4.00", *, contracts=1, entry_id="e1"):
    """ORD-01: the 4-leg opening condor, sold for a net credit."""
    return OrderIntent(
        order_type="limit", tif="Day", kind="iron_condor", entry_id=entry_id,
        contracts=contracts, price=D(str(price)), underlying="SPXW", expiration=EXP,
        idempotency_key=f"entry:{entry_id}",
        legs=condor_legs(put_short=D("5990"), put_long=D("5940"),
                         call_short=D("6060"), call_long=D("6110"), contracts=contracts))


def stop_intent(side="PUT", trigger="3.80", *, contracts=1, entry_id="e1", replaced_from=""):
    """STP-01/06: the resting buy-to-close stop-market on one short."""
    return protective_stop(
        entry_id=entry_id, right="P" if side == "PUT" else "C", contracts=contracts,
        trigger=D(str(trigger)), symbol=PUT_SHORT if side == "PUT" else CALL_SHORT,
        idempotency_key=f"stop:{entry_id}:{side}", replaced_from=replaced_from)


def close_intent(side="PUT", price="0.05", *, contracts=1, entry_id="e1"):
    """CLS-01 / DCY-02: buy the short back."""
    return marketable_close(
        entry_id=entry_id, right="P" if side == "PUT" else "C", contracts=contracts,
        price=D(str(price)), symbol=PUT_SHORT if side == "PUT" else CALL_SHORT,
        idempotency_key=f"close:{entry_id}:{side}")


def lex_intent(side="PUT", price="0.40", *, contracts=1, entry_id="e1"):
    """LEX-01: sell the orphaned long."""
    return OrderIntent(
        order_type="limit", tif="Day", kind="lex", entry_id=entry_id,
        contracts=contracts, price=D(str(price)), idempotency_key=f"lex:{entry_id}:{side}",
        legs=(OrderLeg(right="P" if side == "PUT" else "C", action="sell_to_close",
                       qty=contracts, symbol=PUT_LONG if side == "PUT" else CALL_LONG),))


def any_intent(entry_id="e1"):
    """When the test only needs *an* order and doesn't care what."""
    return condor_intent(entry_id=entry_id)
