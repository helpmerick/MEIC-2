"""EventJournal — the durable, single-stream event log (REC-01 / REC-07(8)).

Distinct from tests/adapters/test_event_store_sqlite.py (SqliteEventStore,
the per-STREAM store). This file exercises the flat global journal backing
`comp.events` and its self-describing codec: EVERY Event subclass defined in
meic.domain.events must survive append -> load exactly, including Decimal,
None (D10-style "absent is absent"), and nested FilledLeg tuples.
"""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from meic.adapters.persistence.event_store import EventJournal, _event_registry
from meic.domain import events as ev


def _sample_instances() -> dict[type, ev.Event]:
    leg = ev.FilledLeg(symbol="SPXW  260709P05600000", right="P", role="short",
                        qty=1, price=D("3.00"))
    return {
        ev.DayArmed: ev.DayArmed(date="2026-07-09", entry_count=3),
        ev.EntryWindowOpened: ev.EntryWindowOpened(date="2026-07-09", entry_number=1),
        ev.EntrySkipped: ev.EntrySkipped(date="2026-07-09", entry_number=1, reason="not_armed"),
        ev.DayCompleted: ev.DayCompleted(date="2026-07-09"),
        ev.ModeSwitchStaged: ev.ModeSwitchStaged(target="live", effective="next_day"),
        ev.CondorProposed: ev.CondorProposed(entry_id="2026-07-09#1", put_short=D("5600"),
                                              call_short=D("5700")),
        ev.CondorFilled: ev.CondorFilled(
            entry_id="2026-07-09#1", net_credit=D("4.00"), fee=D("1.42"),
            short_premium=D("6.00"), legs=(leg,), initiator="manual_entry",
            at="2026-07-09T10:00:00+00:00"),
        ev.StopPlaced: ev.StopPlaced(entry_id="2026-07-09#1", side="PUT", trigger=D("3.80")),
        ev.StopReplaced: ev.StopReplaced(entry_id="2026-07-09#1", side="PUT"),
        ev.ReconciliationMismatch: ev.ReconciliationMismatch(detail="broker mismatch"),
        ev.StopConfirmed: ev.StopConfirmed(entry_id="2026-07-09#1", side="PUT"),
        ev.SideUnprotected: ev.SideUnprotected(entry_id="2026-07-09#1", side="PUT",
                                                action="flatten_side"),
        ev.WatchdogEscalated: ev.WatchdogEscalated(
            entry_id="2026-07-09#1", side="PUT", mark_at_breach=D("3.85"),
            elapsed_seconds=D("20"), fill_price=D("3.90")),
        ev.EntryClosedInfeasible: ev.EntryClosedInfeasible(entry_id="2026-07-09#1"),
        ev.ShortStopped: ev.ShortStopped(
            entry_id="2026-07-09#1", side="PUT", fill=D("3.80"), slippage=D("0.10"),
            fee=D("0.65"), initiator="watchdog_escalation"),
        # STP-08a (v1.61): decay buyback order id journaled at placement.
        ev.DecayBuybackPlaced: ev.DecayBuybackPlaced(
            entry_id="2026-07-09#1", side="PUT", broker_order_id="482621999",
            price=D("0.05")),
        ev.LongSold: ev.LongSold(entry_id="2026-07-09#1", side="PUT", recovery=D("0.50"),
                                  fee=D("0.65")),
        ev.SideClosed: ev.SideClosed(entry_id="2026-07-09#1", side="PUT"),
        ev.SideExpired: ev.SideExpired(entry_id="2026-07-09#1", side="CALL"),
        ev.EntryClosed: ev.EntryClosed(entry_id="2026-07-09#1", initiator="take_profit"),
        ev.LongSaleStarted: ev.LongSaleStarted(entry_id="2026-07-09#1", side="PUT"),
        ev.LongSaleRepriced: ev.LongSaleRepriced(entry_id="2026-07-09#1", side="PUT",
                                                  step=2, price=D("0.45")),
        ev.ForeignDetected: ev.ForeignDetected(symbol="AAPL"),
        ev.ForeignReduction: ev.ForeignReduction(symbol="SPXW  260709P05600000",
                                                  from_qty=2, to_qty=1),
        ev.EntryCompleted: ev.EntryCompleted(entry_id="2026-07-09#1"),
        ev.EntryMarkSample: ev.EntryMarkSample(
            entry_id="2026-07-09#1", at="2026-07-09T10:01:00+00:00", spot=D("5650.25"),
            put_short_mid=D("3.00"), put_long_mid=None, call_short_mid=D("2.50"),
            call_long_mid=D("0.40")),
        ev.DayBrokerConfirmed: ev.DayBrokerConfirmed(
            date="2026-07-09", at="2026-07-09T16:20:00-04:00",
            checked={"fees": "220.00", "flat": "True"}),
        ev.CorrectionRecord: ev.CorrectionRecord(
            date="2026-07-09", field="fees", bot_value="220.00", broker_value="240.00",
            diff="20.00", at="2026-07-09T16:20:00-04:00"),
        # RPT-16 settlement import (operator ruling 2026-07-10): the
        # representative values are a Receive-Deliver cash-settled assignment
        # -- `action` is the broker's transaction_sub_type and `value` its
        # signed NET cash effect (real dollars, already net of the $5 fee).
        # A Trade-style fill row leaves `value` at its None default (see the
        # dedicated None round-trip test below).
        ev.ExternalFillImported: ev.ExternalFillImported(
            day="2026-07-09", at="2026-07-09T22:00:00-04:00", order_id="482390058",
            symbol="SPXW  260709C07540000", action="Cash Settled Assignment", quantity=1,
            price=D("7540.00"), fee=D("5.00"), imported_at="2026-07-10T09:00:00-04:00",
            source="tastytrade_history", value=D("-369.00")),
        # EOD-01 v1.59 (LIVE settlement capture, distinct from the RPT-16
        # import above): the pinned 2026-07-09 C7540 cash-settled assignment.
        ev.SettlementRecorded: ev.SettlementRecorded(
            entry_id="2026-07-09#1", day="2026-07-09", at="2026-07-09T22:00:00-04:00",
            symbol="SPXW  260709C07540000", sub_type="Cash Settled Assignment", quantity=1,
            price=D("7540.00"), value=D("-369.00"), fee=D("5.00"),
            source="tastytrade_receive_deliver"),
    }


def _all_event_subclasses() -> set[type]:
    return {obj for obj in vars(ev).values()
            if isinstance(obj, type) and issubclass(obj, ev.Event) and obj is not ev.Event}


def test_every_event_subclass_has_a_representative_sample():
    """Guards the fixture itself: a newly-added Event subclass with no sample
    here would silently escape the round-trip test below."""
    assert _all_event_subclasses() == set(_sample_instances())


def test_registry_is_introspected_not_hand_listed():
    registry = _event_registry()
    assert set(registry.values()) == _all_event_subclasses()
    assert set(registry.keys()) == {cls.__name__ for cls in _all_event_subclasses()}


def test_round_trip_every_event_subclass_exactly(tmp_path):
    journal = EventJournal(tmp_path / "state.db")
    samples = list(_sample_instances().values())
    for event in samples:
        journal.append(event)

    loaded = journal.load()
    assert len(loaded) == len(samples)
    for original, restored in zip(samples, loaded):
        assert type(restored) is type(original)
        assert restored == original  # frozen dataclass field-by-field equality


def test_entry_mark_sample_all_none_marks_round_trip(tmp_path):
    """D10: absent is absent -- every mark field independently None must
    survive, never coerced to 0 or fabricated."""
    journal = EventJournal(tmp_path / "state.db")
    sample = ev.EntryMarkSample(entry_id="2026-07-09#2", at="2026-07-09T10:02:00+00:00")
    journal.append(sample)
    loaded = journal.load()
    assert loaded == [sample]
    assert loaded[0].spot is None and loaded[0].put_short_mid is None


def test_external_fill_imported_none_price_and_fee_round_trip(tmp_path):
    """RPT-16: a broker fill with no allocated price/fee data is recorded
    honestly as None, never fabricated as 0 -- and a Trade-style row's
    `value` stays at its None default (only Receive-Deliver settlement rows
    carry one, operator ruling 2026-07-10)."""
    journal = EventJournal(tmp_path / "state.db")
    sample = ev.ExternalFillImported(
        day="2026-07-09", at="2026-07-09T14:31:02-04:00", order_id="482214732",
        symbol="SPXW  260709P05600000", action="Sell to Open", quantity=1,
        price=None, fee=None, imported_at="2026-07-10T09:00:00-04:00",
        source="tastytrade_history")
    journal.append(sample)
    loaded = journal.load()
    assert loaded == [sample]
    assert loaded[0].price is None and loaded[0].fee is None
    assert loaded[0].value is None


def test_config_version_round_trips(tmp_path):
    journal = EventJournal(tmp_path / "state.db")
    stamped = ev.DayArmed(date="2026-07-09", entry_count=1).stamped("v1.54")
    journal.append(stamped)
    loaded = journal.load()
    assert loaded[0].config_version == "v1.54"


def test_append_survives_process_death(tmp_path):
    """Crash/restart = close this object, open a new one on the same file."""
    path = tmp_path / "state.db"
    j1 = EventJournal(path)
    j1.append(ev.DayArmed(date="2026-07-09", entry_count=2))
    j1.close()

    j2 = EventJournal(path)
    loaded = j2.load()
    assert len(loaded) == 1 and loaded[0].entry_count == 2


def test_load_preserves_seq_order(tmp_path):
    journal = EventJournal(tmp_path / "state.db")
    for i in range(5):
        journal.append(ev.DayCompleted(date=f"2026-07-{9 + i:02d}"))
    assert [e.date for e in journal.load()] == [f"2026-07-{9 + i:02d}" for i in range(5)]


def test_append_rejects_a_class_not_defined_in_domain_events(tmp_path):
    """An unknown class at APPEND time must raise loudly (never silently
    swallow an object the journal cannot faithfully replay)."""
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _RogueEvent(ev.Event):
        entry_id: str

    journal = EventJournal(tmp_path / "state.db")
    with pytest.raises(ValueError, match="_RogueEvent"):
        journal.append(_RogueEvent(entry_id="x"))


def test_load_skips_unknown_type_with_stderr_warning(tmp_path, capsys):
    """Forward/backward compat: a row whose `type` this build doesn't
    recognize (e.g. a retired event, or one from a newer build) is SKIPPED,
    not raised -- the rest of the log must still replay."""
    path = tmp_path / "state.db"
    journal = EventJournal(path)
    journal.append(ev.DayArmed(date="2026-07-09", entry_count=1))
    journal._conn.execute(
        "INSERT INTO events (at, type, config_version, payload) VALUES (?, ?, ?, ?)",
        ("2026-07-09T00:00:00+00:00", "SomeRetiredEvent", "", "{}"))

    loaded = journal.load()
    assert [type(e).__name__ for e in loaded] == ["DayArmed"]
    assert "SomeRetiredEvent" in capsys.readouterr().err
