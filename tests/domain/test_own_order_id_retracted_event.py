"""OWN-01 append-only retraction: `OwnOrderIdRetracted` -- withdraws a
previously-claimed broker order id (an operator's own out-of-band order
mistakenly journaled as the bot's) from the bot's own-scope. It must
round-trip like every other event AND must be invisible to every money fold
-- see tests/reporting/test_own_order_id_retracted_money_safety.py for the
byte-identical-P&L pin. This file only covers the event shape itself.
"""
from __future__ import annotations

from decimal import Decimal as D

from meic.domain.events import Event, OwnOrderIdRetracted


def test_registers_in_the_event_type_registry():
    assert Event._registry["OwnOrderIdRetracted"] is OwnOrderIdRetracted


def test_round_trips_through_to_dict_from_dict_with_all_fields():
    event = OwnOrderIdRetracted(
        entry_id="2026-07-10#1", broker_order_id="482760202",
        reason="operator's own out-of-band order, not the bot's",
        at="2026-07-14T09:00:00-04:00", note="operator ruling 2026-07-14, strict OWN-01")
    data = event.to_dict()
    restored = Event.from_dict(data)
    assert restored == event
    assert data["type"] == "OwnOrderIdRetracted"


def test_round_trips_with_optional_fields_absent():
    """`at`/`note` are optional -- a minimal record must still round-trip,
    same convention as `OwnOrderIdBackfilled.at`/`.note`. `reason` is
    required (unlike `at`/`note`) -- a retraction with no stated reason is
    exactly the kind of silent correction OWN-01/RPT-15 forbid."""
    event = OwnOrderIdRetracted(
        entry_id="2026-07-10#1", broker_order_id="482760202", reason="operator's own order")
    restored = Event.from_dict(event.to_dict())
    assert restored == event
    assert restored.at is None
    assert restored.note is None


def test_carries_no_decimal_money_field_at_all():
    """Structural pin for "metadata-only": the event's own dataclass fields
    contain not a single Decimal -- there is nothing here a money fold could
    even accidentally sum."""
    from dataclasses import fields

    event = OwnOrderIdRetracted(entry_id="d#1", broker_order_id="1", reason="r")
    for f in fields(event):
        assert f.type not in ("Decimal", D), (
            f"{f.name} is a Decimal field -- OwnOrderIdRetracted must carry no money")
