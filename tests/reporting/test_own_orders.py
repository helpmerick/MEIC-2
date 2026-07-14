"""`reporting/own_orders.py::own_order_ids` -- OWN-01/OWN-03's ONE pure
definition of "every broker order id the bot itself journaled placing",
shared by `application/report_reconciler.py` (which cannot import
`meic.adapters`) and `adapters/api/server.py::_journaled_own_order_ids`
(which delegates to it -- see server.py's docstring).
"""
from decimal import Decimal as D

from meic.domain.events import (
    CondorFilled,
    DecayBuybackPlaced,
    LexOrderPlaced,
    OwnOrderIdBackfilled,
    OwnOrderIdRetracted,
    StopPlaced,
)
from meic.reporting.own_orders import own_order_ids


def test_reads_broker_order_id_off_condor_filled():
    """OWN-01/OWN-03 (2026-07-11 incident fix): the entry's own order id,
    journaled at fill time, must count as the bot's own -- this is the field
    whose absence let a shared-account reconcile sum the operator's foreign
    trades into "broker truth" (see report_reconciler.py's module docstring)."""
    events = [CondorFilled(entry_id="d#1", net_credit=D("4.00"), broker_order_id="482621396")]
    assert own_order_ids(events) == {"482621396"}


def test_reads_broker_order_id_off_every_carrying_event_type():
    events = [
        CondorFilled(entry_id="d#1", net_credit=D("4.00"), broker_order_id="482621396"),
        StopPlaced(entry_id="d#1", side="PUT", trigger=D("3.80"), broker_order_id="482621556"),
        DecayBuybackPlaced(entry_id="d#1", side="CALL", broker_order_id="482700001", price=D("0.10")),
        LexOrderPlaced(entry_id="d#1", side="PUT", broker_order_id="482760202",
                       price=D("0.05"), kind="fallback"),
    ]
    assert own_order_ids(events) == {"482621396", "482621556", "482700001", "482760202"}


def test_none_broker_order_id_is_never_counted():
    """Every pre-journaling event (or a caller that hasn't threaded the id
    through) carries `broker_order_id=None` -- must never show up as a
    stringified "None" id."""
    events = [CondorFilled(entry_id="d#1", net_credit=D("4.00"))]
    assert own_order_ids(events) == set()


def test_an_event_carrying_no_such_field_at_all_is_skipped_not_errored():
    from meic.domain.events import DayArmed

    events = [DayArmed(date="2026-07-09", entry_count=1)]
    assert own_order_ids(events) == set()


def test_empty_log_is_empty_set():
    assert own_order_ids([]) == set()


# --- OWN-01 retraction (2026-07-14): claimed - retracted --------------------
#
# THE TRAP: `OwnOrderIdRetracted` itself carries a `broker_order_id` field --
# the exact field `own_order_ids` scans for generically on every event -- so
# a naive implementation that just harvests `broker_order_id` off ANYTHING
# would re-claim the very id the retraction exists to withdraw. This is the
# real 2026-07-10 CALL-side incident: the operator's own order 482760202 was
# mistakenly backfilled as the bot's LEX order, then retracted -- the id must
# come out, not go back in.

def test_backfilled_then_retracted_id_is_excluded_the_trap():
    events = [
        OwnOrderIdBackfilled(entry_id="d#1", broker_order_id="482760202", role="lex"),
        OwnOrderIdRetracted(entry_id="d#1", broker_order_id="482760202",
                            reason="operator's own out-of-band order, not the bot's"),
    ]
    result = own_order_ids(events)
    assert "482760202" not in result
    assert result == set()


def test_retraction_order_independent_on_replay():
    """Same outcome whether the retraction is replayed before or after the
    claim in the in-memory list -- the set difference doesn't care about
    order."""
    claim = OwnOrderIdBackfilled(entry_id="d#1", broker_order_id="482760202", role="lex")
    retraction = OwnOrderIdRetracted(entry_id="d#1", broker_order_id="482760202", reason="r")

    assert own_order_ids([claim, retraction]) == own_order_ids([retraction, claim])
    assert own_order_ids([claim, retraction]) == set()


def test_retracting_an_id_never_claimed_is_a_harmless_no_op():
    events = [
        OwnOrderIdBackfilled(entry_id="d#1", broker_order_id="482621396", role="entry"),
        OwnOrderIdRetracted(entry_id="d#1", broker_order_id="999999999", reason="never claimed"),
    ]
    assert own_order_ids(events) == {"482621396"}


def test_appending_the_retraction_twice_is_idempotent():
    claim = OwnOrderIdBackfilled(entry_id="d#1", broker_order_id="482760202", role="lex")
    retraction = OwnOrderIdRetracted(entry_id="d#1", broker_order_id="482760202", reason="r")

    once = own_order_ids([claim, retraction])
    twice = own_order_ids([claim, retraction, retraction])
    assert once == twice == set()


def test_other_ids_on_the_same_entry_are_unaffected_by_a_retraction():
    events = [
        CondorFilled(entry_id="d#1", net_credit=D("4.00"), broker_order_id="482621396"),
        StopPlaced(entry_id="d#1", side="CALL", trigger=D("3.80"), broker_order_id="482621556"),
        OwnOrderIdBackfilled(entry_id="d#1", broker_order_id="482760202", role="lex"),
        OwnOrderIdRetracted(entry_id="d#1", broker_order_id="482760202",
                            reason="operator's own out-of-band order, not the bot's"),
    ]
    # The entry order and the stop order remain claimed; only the retracted
    # lex id is withdrawn.
    assert own_order_ids(events) == {"482621396", "482621556"}


def test_a_directly_journaled_id_can_also_be_retracted_not_just_a_backfilled_one():
    """The trap applies regardless of HOW the id was first claimed -- a
    directly-journaled `broker_order_id` (e.g. on `LexOrderPlaced`) must be
    withdrawable exactly like a backfilled one."""
    events = [
        LexOrderPlaced(entry_id="d#1", side="CALL", broker_order_id="482760202",
                       price=D("0.10"), kind="fallback"),
        OwnOrderIdRetracted(entry_id="d#1", broker_order_id="482760202", reason="r"),
    ]
    assert own_order_ids(events) == set()
