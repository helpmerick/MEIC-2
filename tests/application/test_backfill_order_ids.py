"""OWN-03 / RPT-16 escape hatch: `application/backfill_order_ids.py`'s
`backfill_own_order_ids` -- a one-off, IDEMPOTENT append of
`OwnOrderIdBackfilled` events for an entry whose original events predate
order-id journaling. Pure event append: no broker calls, no I/O.
"""
from __future__ import annotations

from meic.application.backfill_order_ids import backfill_own_order_ids
from meic.domain.events import DayArmed, OwnOrderIdBackfilled

ENTRY_ID = "2026-07-10#1"
AT = "2026-07-12T09:00:00-04:00"
NOTE = "operator-authorised backfill, RPT-16"


def test_appends_one_event_per_id_and_returns_the_count():
    events: list = []
    appended = backfill_own_order_ids(
        events, ENTRY_ID,
        [("482621396", "entry"), ("482621556", "stop"), ("482760202", "lex")],
        at=AT, note=NOTE)

    assert appended == 3
    backfilled = [e for e in events if isinstance(e, OwnOrderIdBackfilled)]
    assert len(backfilled) == 3
    assert {(e.broker_order_id, e.role) for e in backfilled} == {
        ("482621396", "entry"), ("482621556", "stop"), ("482760202", "lex")}
    assert all(e.entry_id == ENTRY_ID for e in backfilled)
    assert all(e.at == AT and e.note == NOTE for e in backfilled)


def test_running_twice_appends_only_once():
    events: list = []
    ids = [("482621396", "entry"), ("482621556", "stop"), ("482760202", "lex")]

    first = backfill_own_order_ids(events, ENTRY_ID, ids, at=AT, note=NOTE)
    second = backfill_own_order_ids(events, ENTRY_ID, ids, at=AT, note=NOTE)

    assert first == 3
    assert second == 0
    assert len([e for e in events if isinstance(e, OwnOrderIdBackfilled)]) == 3


def test_a_partially_new_id_only_appends_the_new_one():
    events: list = []
    backfill_own_order_ids(events, ENTRY_ID, [("482621396", "entry")], at=AT, note=NOTE)

    appended = backfill_own_order_ids(
        events, ENTRY_ID,
        [("482621396", "entry"), ("482621556", "stop")],
        at=AT, note=NOTE)

    assert appended == 1
    backfilled = [e for e in events if isinstance(e, OwnOrderIdBackfilled)]
    assert len(backfilled) == 2


def test_does_not_touch_unrelated_pre_existing_events():
    pre_existing = DayArmed(date="2026-07-10", entry_count=1)
    events: list = [pre_existing]
    backfill_own_order_ids(events, ENTRY_ID, [("482621396", "entry")], at=AT, note=NOTE)

    assert events[0] is pre_existing
    assert len(events) == 2


def test_never_calls_any_broker_or_io():
    """Structural pin (mirrors test_backfill_structural.py / RPT-16 rule 6):
    the module imports nothing from meic.adapters or meic.composition."""
    import meic.application.backfill_order_ids as mod

    src = mod.__file__
    with open(src, encoding="utf-8") as f:
        text = f.read()
    assert "meic.adapters" not in text
    assert "meic.composition" not in text
