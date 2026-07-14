"""close_assembly.py / CloseEntry -- the DCY-01/CLS-01 double-order regression
(2026-07-14, found in review of the DecayWatcher live-wiring change).

`DecayWatcher.buyback()` cancels a short's resting protective stop and
replaces it with a WORKING `kind="decay"` limit buy-to-close
(application/decay_watcher.py). Before this fix, `assemble_close_inputs`
only ever recognized `order_type == "stop_market"` as "the resting thing to
replace" -- so any close routed through the canonical path (manual Close,
Flatten All, TPF/TPT, EOD auto-close; ALL of them call
`assemble_close_inputs` per its own module docstring) while a decay buyback
was still resting saw NO stop for that side and took CLS-01(3)'s "no resting
stop" branch: a DIRECT marketable buy-to-close submitted with no cancel/
replace at all. That leaves TWO live buy-to-close orders resting on the same
short leg simultaneously -- a genuine double-fill-into-a-net-long race on
real money. This was unreachable in production before this week because
DecayWatcher was never constructed anywhere; wiring it into live_app() made
it reachable for the first time.

These tests pin the fix: a working decay buyback is folded into
`assemble_close_inputs`'s `resting_stop_ids`, so `CloseEntry.close()` routes
it through the SAME race-safe `broker.replace()` path an ordinary resting
stop gets -- never a naked second order.
"""
import asyncio
from decimal import Decimal as D

from meic.application.close_entry import CloseEntry
from meic.application.decay_watcher import DecayWatcher
from meic.application.order_intent import protective_stop
from meic.domain.events import CondorFilled, FilledLeg
from meic.composition.close_assembly import DEFAULT_CLOSE_PRICE, assemble_close_inputs
from tests.harness.fake_broker import FakeBroker

ENTRY = "e1"
SHORT_SYM = "SPXW  260714P07535000"
LONG_SYM = "SPXW  260714P07510000"


def _condor_filled_legs():
    return (
        FilledLeg(symbol=LONG_SYM, right="P", role="long", qty=1, price=D("0.50")),
        FilledLeg(symbol=SHORT_SYM, right="P", role="short", qty=1, price=D("3.00")),
    )


def _entry_with_in_flight_decay_buyback():
    """A short leg whose resting stop DecayWatcher.buyback() already cancelled
    and replaced with a working decay limit order -- exactly the state
    `_decay_watcher_pass`'s in-flight branch leaves a side in."""
    events = [CondorFilled(entry_id=ENTRY, net_credit=D("3.50"), legs=_condor_filled_legs())]
    broker = FakeBroker()
    stop_id = asyncio.run(broker.submit(protective_stop(
        entry_id=ENTRY, right="P", contracts=1, trigger=D("3.80"), symbol=SHORT_SYM,
        idempotency_key=f"stop:{ENTRY}:PUT")))
    watcher = DecayWatcher(broker, events)
    outcome = asyncio.run(watcher.buyback(
        entry_id=ENTRY, side="PUT", resting_stop_id=stop_id, symbol=SHORT_SYM))
    assert outcome != "STOP_FILLED_RUN_LEX"
    return events, broker, outcome  # outcome is the decay buyback's own order id


def test_assemble_close_inputs_recognizes_a_working_decay_buyback_as_the_resting_stop():
    events, broker, buyback_id = _entry_with_in_flight_decay_buyback()

    legs, stop_ids = asyncio.run(assemble_close_inputs(events, broker, ENTRY))

    assert stop_ids.get("PUT") == buyback_id, (
        "a working decay buyback must be folded into resting_stop_ids so CloseEntry "
        "replaces it, never treats the side as stop-free")


def test_close_entry_replaces_the_decay_buyback_never_submits_a_second_naked_order():
    events, broker, buyback_id = _entry_with_in_flight_decay_buyback()
    close_entry = CloseEntry(broker, events)

    legs, stop_ids = asyncio.run(assemble_close_inputs(events, broker, ENTRY))
    asyncio.run(close_entry.close(
        ENTRY, "manual", resting_stop_ids=stop_ids, live_legs=legs, close_price=DEFAULT_CLOSE_PRICE))

    put_buy_to_closes = [
        o for o in broker._orders.values()
        if o.intent.legs and o.intent.legs[0].right == "P"
        and o.intent.legs[0].action == "buy_to_close"
    ]
    working_put_buy_to_closes = [o for o in put_buy_to_closes if o.status == "WORKING"]
    assert len(working_put_buy_to_closes) == 1, (
        f"exactly one live buy-to-close must rest on the PUT short at a time, "
        f"got {[ (o.order_id, o.status) for o in put_buy_to_closes ]}")
    # the ORIGINAL decay buyback must have been replaced (no longer WORKING) --
    # CLS-01's replace(), not an ad-hoc second submit alongside it.
    decay_order = broker._orders[buyback_id]
    assert decay_order.status in ("REPLACED", "CANCELLED", "FILLED")


def test_a_genuine_resting_stop_wins_over_a_decay_buyback_for_the_same_side():
    """Review finding (2026-07-14, non-blocking): the two should never
    structurally coexist (DCY-02(1) cancels the stop before placing the
    buyback), but the correlation must not depend on whichever
    `working_orders()` happens to list last -- a genuine resting stop is
    always the one CLS-01 replaces."""
    events, broker, buyback_id = _entry_with_in_flight_decay_buyback()
    # A fresh stop somehow ALSO ends up resting for the same side (defensive
    # scenario only -- not a reachable production state).
    stop_id = asyncio.run(broker.submit(protective_stop(
        entry_id=ENTRY, right="P", contracts=1, trigger=D("3.80"), symbol=SHORT_SYM,
        idempotency_key=f"stop:{ENTRY}:PUT:2")))

    _legs, stop_ids = asyncio.run(assemble_close_inputs(events, broker, ENTRY))

    assert stop_ids.get("PUT") == stop_id, "a genuine resting stop must win over a decay buyback"
    assert stop_ids.get("PUT") != buyback_id
