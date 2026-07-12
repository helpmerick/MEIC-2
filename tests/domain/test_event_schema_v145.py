"""The v1.44/v1.45 event schema migration: config_version stamping (v1.44,
"build it NOW, not debt") and ORD-09 leg identity (v1.45) — one migration.

An event log without version stamps cannot be audited after the fact: config is
"next-entry" scoped (doc 06), so a day can legitimately contain entries taken
under a 95% stop and entries taken under 150%. Without the stamp you cannot tell
which rules produced which event.
"""
import json
from decimal import Decimal as D

from meic.application.event_log import EventLog
from meic.domain.events import (
    CondorFilled,
    DayArmed,
    Event,
    EntrySkipped,
    FilledLeg,
    StopPlaced,
)

LEGS = (FilledLeg("SPXW  260706P05940000", "P", "long", 2),
        FilledLeg("SPXW  260706P05990000", "P", "short", 2, D("1.35")),
        FilledLeg("SPXW  260706C06060000", "C", "short", 2, D("1.25")),
        FilledLeg("SPXW  260706C06110000", "C", "long", 2))


# --- ORD-09 legs survive the log round-trip -------------------------------------

def test_condor_filled_round_trips_its_legs_through_json():
    """REC-01: a replayed log must reconstruct the event exactly. The legs are the
    ONLY record of what instruments we own — losing them on replay would leave a
    recovered bot unable to name its own positions."""
    original = CondorFilled(entry_id="d#1", net_credit=D("4.00"), legs=LEGS)

    revived = Event.from_dict(json.loads(json.dumps(original.to_dict())))

    assert revived == original
    assert revived.legs == LEGS
    assert revived.legs[1].price == D("1.35")      # exact, via str — never float
    assert revived.legs[0].price is None           # "no broker allocation" survives


def test_leg_prices_serialize_as_strings_not_floats():
    d = CondorFilled(entry_id="d#1", net_credit=D("4.00"), legs=LEGS).to_dict()
    assert d["legs"][1]["price"] == "1.35"
    assert d["legs"][0]["price"] is None
    assert d["net_credit"] == "4.00"


def test_an_older_log_entry_without_legs_still_replays():
    """Schema evolution: events written before ORD-09 have no `legs` key."""
    old = {"type": "CondorFilled", "entry_id": "d#1", "net_credit": "4.00"}
    revived = Event.from_dict(old)
    assert revived.legs == () and revived.net_credit == D("4.00")


def test_filled_leg_side_maps_right_to_put_call():
    assert LEGS[1].side == "PUT" and LEGS[2].side == "CALL"


# --- config_version stamping -----------------------------------------------------

def test_the_event_log_stamps_every_appended_event():
    log = EventLog(config_version="v1.45")
    log.append(DayArmed(date="2026-07-06", entry_count=3))
    log.append(CondorFilled(entry_id="d#1", net_credit=D("4.00"), legs=LEGS))
    log.extend([EntrySkipped(date="2026-07-06", entry_number=2, reason="max_day_risk")])
    assert [e.config_version for e in log] == ["v1.45"] * 3


def test_the_stamp_survives_the_json_round_trip():
    e = StopPlaced(entry_id="d#1", side="PUT", trigger=D("3.80")).stamped("v1.45")
    revived = Event.from_dict(json.loads(json.dumps(e.to_dict())))
    assert revived.config_version == "v1.45"


def test_events_recorded_after_a_midday_config_save_carry_the_new_version():
    """Config is next-entry scoped: a day can hold entries under different rules."""
    log = EventLog(config_version="v1")
    log.append(DayArmed(date="2026-07-06", entry_count=2))
    log.config_version = "v2"                       # operator saved new config
    log.append(EntrySkipped(date="2026-07-06", entry_number=2, reason="stop_trading"))
    assert [e.config_version for e in log] == ["v1", "v2"]


def test_an_already_stamped_event_is_not_restamped():
    """A replayed event keeps the version it was RECORDED under, not the one in
    force when it was re-appended."""
    log = EventLog(config_version="v2")
    log.append(DayArmed(date="2026-07-06", entry_count=1).stamped("v1"))
    assert log[0].config_version == "v1"


def test_stamping_never_mutates_the_callers_event():
    original = DayArmed(date="2026-07-06", entry_count=1)
    stamped = original.stamped("v1.45")
    assert original.config_version == "" and stamped.config_version == "v1.45"
    assert stamped == original                       # same FACT, different provenance


def test_config_version_is_not_a_dataclass_field():
    """Two events differing only in config version are the same fact, so it must
    not enter __eq__ — and no service should have to pass it to a constructor."""
    a = DayArmed(date="2026-07-06", entry_count=1).stamped("v1")
    b = DayArmed(date="2026-07-06", entry_count=1).stamped("v2")
    assert a == b
    assert "config_version" not in DayArmed(date="d", entry_count=1).to_dict()  # unstamped: absent


def test_an_unstamped_log_behaves_exactly_like_a_plain_list():
    log = EventLog()                                 # no config_version configured
    log.append(DayArmed(date="2026-07-06", entry_count=1))
    assert log[0].config_version == "" and isinstance(log, list)
