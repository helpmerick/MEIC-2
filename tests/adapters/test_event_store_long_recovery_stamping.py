"""RPT-07 long-recovery mark-at-stop/markup stamping (2026-07-11) round-trips
through EventJournal exactly like `StopPlaced.broker_order_id`'s (v1.60)
precedent: optional/additive fields, encoded via `_encode_value`'s runtime-
value tagging (not the fragile string-type-annotation path `Event.to_dict`/
`from_dict` uses — see event_store.py's module docstring), so an
Optional[Decimal] round-trips as a real Decimal, and a field ABSENT from an
old journal row (pre-stamping event) decodes to the dataclass default (None),
never a crash and never a fabricated value."""
import json
from decimal import Decimal as D

from meic.adapters.persistence.event_store import EventJournal, decode_event, encode_event
from meic.domain.events import LongSaleStarted, LongSold, StopPlaced


def test_long_sale_started_stamps_round_trip_through_the_journal(tmp_path):
    journal = EventJournal(tmp_path / "state.db")
    journal.append(LongSaleStarted(
        entry_id="e1", side="PUT",
        mark_bid=D("2.00"), mark_ask=D("2.30"), intrinsic=D("0")))

    loaded = journal.load()
    assert len(loaded) == 1
    e = loaded[0]
    assert isinstance(e, LongSaleStarted)
    assert e.mark_bid == D("2.00") and isinstance(e.mark_bid, D)
    assert e.mark_ask == D("2.30") and isinstance(e.mark_ask, D)
    assert e.intrinsic == D("0") and isinstance(e.intrinsic, D)


def test_stop_placed_markup_round_trips_through_the_journal(tmp_path):
    journal = EventJournal(tmp_path / "state.db")
    journal.append(StopPlaced(entry_id="e1", side="PUT", trigger=D("3.80"),
                              broker_order_id="O-1", markup=D("0.10")))

    loaded = journal.load()
    e = loaded[0]
    assert isinstance(e, StopPlaced)
    assert e.markup == D("0.10") and isinstance(e.markup, D)
    assert e.broker_order_id == "O-1"


def test_pre_stamping_long_sale_started_replays_as_none_not_a_crash():
    """A journal row recorded before this stamping shipped has no
    mark_bid/mark_ask/intrinsic keys at all in its payload -- decode_event
    must fall back to the dataclass default (None), exactly like
    StopPlaced.broker_order_id's v1.60 precedent, never raise and never
    invent a mark."""
    old_row = {"type": "LongSaleStarted", "entry_id": "e1", "side": "PUT"}
    event = decode_event(old_row)
    assert isinstance(event, LongSaleStarted)
    assert event.mark_bid is None
    assert event.mark_ask is None
    assert event.intrinsic is None


def test_pre_stamping_stop_placed_replays_as_none_not_a_crash():
    old_row = {"type": "StopPlaced", "entry_id": "e1", "side": "PUT",
               "trigger": {"__decimal__": "3.80"}}
    event = decode_event(old_row)
    assert isinstance(event, StopPlaced)
    assert event.markup is None
    assert event.broker_order_id is None  # v1.60's own precedent, unaffected


def test_encode_then_decode_a_mixed_old_and_new_log_is_stable(tmp_path):
    """A realistic replay: an old-format LongSaleStarted (dict written before
    this shipped, no new keys) sits in the SAME journal as a new one -- both
    must load without error, and only the new one carries a mark."""
    journal = EventJournal(tmp_path / "state.db")
    journal.append(LongSaleStarted(entry_id="e1", side="PUT"))  # pre-stamping shape
    journal.append(LongSaleStarted(entry_id="e2", side="CALL",
                                   mark_bid=D("1.50"), mark_ask=D("1.70"), intrinsic=D("0.25")))
    journal.append(LongSold(entry_id="e2", side="CALL", recovery=D("1.55")))

    loaded = journal.load()
    assert [type(e).__name__ for e in loaded] == ["LongSaleStarted", "LongSaleStarted", "LongSold"]
    assert loaded[0].mark_bid is None
    assert loaded[1].mark_bid == D("1.50")
    assert loaded[2].recovery == D("1.55")


def test_encode_event_is_json_serializable_for_optional_decimal_fields(tmp_path):
    """Sanity check on the wire shape itself: `encode_event` must produce a
    plain JSON-serializable dict even when the new optional Decimal fields
    are None (the default) -- json.dumps must not choke on it."""
    payload = encode_event(LongSaleStarted(entry_id="e1", side="PUT"))
    raw = json.dumps(payload)  # raises if not serializable
    assert json.loads(raw)["mark_bid"] is None
