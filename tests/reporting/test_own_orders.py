"""`reporting/own_orders.py::own_order_ids` -- OWN-01/OWN-03's ONE pure
definition of "every broker order id the bot itself journaled placing",
shared by `application/report_reconciler.py` (which cannot import
`meic.adapters`) and `adapters/api/server.py::_journaled_own_order_ids`
(which delegates to it -- see server.py's docstring).
"""
from decimal import Decimal as D

from meic.domain.events import CondorFilled, DecayBuybackPlaced, LexOrderPlaced, StopPlaced
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
