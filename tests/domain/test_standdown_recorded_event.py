"""OWN-12 (v1.67, highest-priority open item): `StanddownRecorded` -- every
OWN-09/10/11 standdown appends this event, naming the entry, the leg/side,
the reason, and the broker finding (TC-OWN-12 scenario 1). This file covers
only the event's shape/round-trip; see:
  * tests/reporting/test_standdown_recorded_money_safety.py -- money-neutral pin
  * tests/application/test_stop_fill_watch.py -- the real journaling call site
  * tests/application/test_lex_ladder_watchdog_standdown_absence.py -- TC-OWN-12
    scenario 2, the deliberate false-alarm/no-suppression pin
"""
from __future__ import annotations

from decimal import Decimal as D

from meic.domain.events import Event, StanddownRecorded


def test_registers_in_the_event_type_registry():
    assert Event._registry["StanddownRecorded"] is StanddownRecorded


def test_round_trips_through_to_dict_from_dict_with_all_fields():
    event = StanddownRecorded(
        entry_id="2026-07-10#1", side="CALL",
        reason="long_not_held_at_broker",
        broker_finding="broker reports no open position in SPXW  260710C07570000",
        at="2026-07-14T09:00:00+00:00")
    data = event.to_dict()
    restored = Event.from_dict(data)
    assert restored == event
    assert data["type"] == "StanddownRecorded"


def test_round_trips_with_at_absent():
    """`at` is additive/optional (ORD-11) -- a record from a caller with no
    clock threaded through must still round-trip."""
    event = StanddownRecorded(
        entry_id="2026-07-10#1", side="CALL",
        reason="long_not_held_at_broker", broker_finding="position absent")
    restored = Event.from_dict(event.to_dict())
    assert restored == event
    assert restored.at is None


def test_names_entry_leg_reason_and_broker_finding():
    """TC-OWN-12 scenario 1: 'a standdown event enters the journal naming
    entry, leg, reason, and broker finding' -- structural pin that all four
    are real, distinct fields on the event (not folded into one string)."""
    event = StanddownRecorded(
        entry_id="2026-07-10#1", side="CALL",
        reason="long_not_held_at_broker",
        broker_finding="broker reports no open position in SPXW  260710C07570000")
    assert event.entry_id == "2026-07-10#1"
    assert event.side == "CALL"
    assert event.reason == "long_not_held_at_broker"
    assert "no open position" in event.broker_finding


def test_carries_no_decimal_money_field_at_all():
    """Structural pin for "metadata-only": not a single Decimal field -- there
    is nothing here a money fold could even accidentally sum."""
    from dataclasses import fields

    event = StanddownRecorded(entry_id="d#1", side="PUT", reason="r", broker_finding="f")
    for f in fields(event):
        assert f.type not in ("Decimal", D), (
            f"{f.name} is a Decimal field -- StanddownRecorded must carry no money")
