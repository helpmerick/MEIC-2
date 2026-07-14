"""OWN-01 append-only retraction: `application/retract_own_order_id.py`'s
`retract_own_order_ids` -- a one-off, IDEMPOTENT append of
`OwnOrderIdRetracted` events for a broker order id mistakenly claimed as the
bot's own. Pure event append: no broker calls, no I/O. Mirrors
tests/application/test_backfill_order_ids.py.
"""
from __future__ import annotations

from meic.application.retract_own_order_id import retract_own_order_ids
from meic.domain.events import DayArmed, OwnOrderIdRetracted

ENTRY_ID = "2026-07-10#1"
AT = "2026-07-14T09:00:00-04:00"
NOTE = "operator ruling 2026-07-14, strict OWN-01"
LEX_ID = "482760202"
REASON = "operator's own out-of-band order, not the bot's"


def test_appends_one_event_per_id_and_returns_the_count():
    events: list = []
    appended = retract_own_order_ids(
        events, ENTRY_ID, [(LEX_ID, REASON)], at=AT, note=NOTE)

    assert appended == 1
    retracted = [e for e in events if isinstance(e, OwnOrderIdRetracted)]
    assert len(retracted) == 1
    assert retracted[0].broker_order_id == LEX_ID
    assert retracted[0].entry_id == ENTRY_ID
    assert retracted[0].reason == REASON
    assert retracted[0].at == AT and retracted[0].note == NOTE


def test_running_twice_appends_only_once():
    events: list = []
    ids = [(LEX_ID, REASON)]

    first = retract_own_order_ids(events, ENTRY_ID, ids, at=AT, note=NOTE)
    second = retract_own_order_ids(events, ENTRY_ID, ids, at=AT, note=NOTE)

    assert first == 1
    assert second == 0
    assert len([e for e in events if isinstance(e, OwnOrderIdRetracted)]) == 1


def test_a_partially_new_id_only_appends_the_new_one():
    events: list = []
    retract_own_order_ids(events, ENTRY_ID, [(LEX_ID, REASON)], at=AT, note=NOTE)

    appended = retract_own_order_ids(
        events, ENTRY_ID,
        [(LEX_ID, REASON), ("482621556", "also foreign")],
        at=AT, note=NOTE)

    assert appended == 1
    retracted = [e for e in events if isinstance(e, OwnOrderIdRetracted)]
    assert len(retracted) == 2


def test_does_not_touch_unrelated_pre_existing_events():
    pre_existing = DayArmed(date="2026-07-10", entry_count=1)
    events: list = [pre_existing]
    retract_own_order_ids(events, ENTRY_ID, [(LEX_ID, REASON)], at=AT, note=NOTE)

    assert events[0] is pre_existing
    assert len(events) == 2


def test_retracting_an_id_never_claimed_is_a_harmless_no_op():
    """The retraction target need not have been claimed at all -- it is
    still appended (the intent is recorded), and `own_order_ids` simply has
    nothing to subtract it from."""
    events: list = []
    appended = retract_own_order_ids(
        events, ENTRY_ID, [("999999999", "never claimed")], at=AT, note=NOTE)
    assert appended == 1


def test_never_calls_any_broker_or_io():
    """Structural pin (mirrors test_backfill_order_ids.py /
    test_backfill_structural.py): the module imports nothing from
    meic.adapters or meic.composition."""
    import meic.application.retract_own_order_id as mod

    src = mod.__file__
    with open(src, encoding="utf-8") as f:
        text = f.read()
    assert "meic.adapters" not in text
    assert "meic.composition" not in text
