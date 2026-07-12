"""OWN-03 / RPT-16 escape hatch: `OwnOrderIdBackfilled` is a METADATA-ONLY
event -- an operator-supplied broker order id for an entry whose original
events predate order-id journaling. It must round-trip like every other
event AND must be invisible to every money fold (`domain.projection.fold`,
`reporting.folds.core_results`) -- see
tests/reporting/test_own_order_id_backfilled_money_safety.py for the
byte-identical-P&L pin. This file only covers the event shape itself.
"""
from __future__ import annotations

from decimal import Decimal as D

from meic.domain.events import Event, OwnOrderIdBackfilled


def test_registers_in_the_event_type_registry():
    assert Event._registry["OwnOrderIdBackfilled"] is OwnOrderIdBackfilled


def test_round_trips_through_to_dict_from_dict_with_all_fields():
    event = OwnOrderIdBackfilled(
        entry_id="2026-07-10#1", broker_order_id="482621396", role="entry",
        at="2026-07-12T09:00:00-04:00", note="operator-authorised backfill")
    data = event.to_dict()
    restored = Event.from_dict(data)
    assert restored == event
    assert data["type"] == "OwnOrderIdBackfilled"


def test_round_trips_with_optional_fields_absent():
    """`at`/`note` are optional -- a minimal record must still round-trip,
    same convention as `StopPlaced.broker_order_id` etc."""
    event = OwnOrderIdBackfilled(
        entry_id="2026-07-10#1", broker_order_id="482621556", role="stop")
    restored = Event.from_dict(event.to_dict())
    assert restored == event
    assert restored.at is None
    assert restored.note is None


def test_carries_no_decimal_money_field_at_all():
    """Structural pin for "metadata-only": the event's own dataclass fields
    contain not a single Decimal -- there is nothing here a money fold could
    even accidentally sum."""
    from dataclasses import fields

    event = OwnOrderIdBackfilled(entry_id="d#1", broker_order_id="1", role="lex")
    for f in fields(event):
        assert f.type not in ("Decimal", D), (
            f"{f.name} is a Decimal field -- OwnOrderIdBackfilled must carry no money")
