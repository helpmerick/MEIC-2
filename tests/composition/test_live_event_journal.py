"""LiveComposition wiring for the durable event log (REC-01 / REC-07(8)).

Today's gap this closes: `comp.events` used to be a plain in-memory
`EventLog` even in the live composition, so a restart lost the whole event
log. When `state_store` is a real `SqliteStateStore`, `comp.events` must be a
`DurableEventLog` write-through to an `EventJournal` in the SAME state.db,
pre-loaded with whatever the journal already held BEFORE any service is
built around it (boot restore) -- and the restored log must still feed the
pure projection folds (day_report sees the fill), not just replay inert.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from decimal import Decimal as D

from meic.adapters.persistence.event_store import InMemoryStateStore, SqliteStateStore
from meic.application.clocks import MutableClock
from meic.application.event_log import DurableEventLog, EventLog
from meic.composition.live import LiveComposition
from meic.domain.events import CondorFilled, DayArmed
from meic.domain.projection import day_report
from meic.domain.ticks import TickRung, TickTable

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


def _cert_jwt() -> str:
    """TastytradeAdapter refuses a non-cert refresh-token issuer before any
    network call (assert_cert_token) -- construct a token whose payload
    passes that check, same pattern as tests/application/test_live_app.py."""
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'EdDSA'})}.{seg({'iss': 'https://api.sandbox.tastyworks.com'})}.sig"


def _live_comp(state_store):
    return LiveComposition(
        clock=MutableClock(datetime(2026, 7, 9, 14, 30, tzinfo=timezone.utc)),
        ticks=SPX, provider_secret="s", refresh_token=_cert_jwt(), state_store=state_store)


def test_events_is_durable_when_state_store_is_sqlite(tmp_path):
    comp = _live_comp(SqliteStateStore(tmp_path / "state.db"))
    assert isinstance(comp.events, DurableEventLog)


def test_events_stays_in_memory_when_state_store_is_not_sqlite():
    """No SqliteStateStore injected (e.g. an offline/unit-test composition) ->
    unchanged behaviour, plain EventLog, no db file touched."""
    comp = _live_comp(InMemoryStateStore())
    assert type(comp.events) is EventLog
    assert not isinstance(comp.events, DurableEventLog)


def test_appended_event_survives_composition_rebuild_and_still_folds(tmp_path):
    comp = _live_comp(SqliteStateStore(tmp_path / "state.db"))
    comp.events.append(DayArmed(date="2026-07-09", entry_count=1))
    comp.events.append(CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00")))

    # "restart": a brand-new composition opened on the SAME db file.
    rebuilt = _live_comp(SqliteStateStore(tmp_path / "state.db"))

    assert [type(e).__name__ for e in rebuilt.events] == ["DayArmed", "CondorFilled"]
    assert rebuilt.events[1].net_credit == D("4.00")

    # REC-01's whole point: the restored log still feeds the pure fold.
    report = day_report(rebuilt.events)
    assert report.total_credit == D("4.00")
    assert report.entries_filled == 1


def test_events_appended_after_rebuild_are_also_durable(tmp_path):
    path = tmp_path / "state.db"
    comp1 = _live_comp(SqliteStateStore(path))
    comp1.events.append(DayArmed(date="2026-07-09", entry_count=1))

    comp2 = _live_comp(SqliteStateStore(path))
    comp2.events.append(CondorFilled(entry_id="2026-07-09#1", net_credit=D("2.50")))

    comp3 = _live_comp(SqliteStateStore(path))
    assert [type(e).__name__ for e in comp3.events] == ["DayArmed", "CondorFilled"]
    assert comp3.events[1].net_credit == D("2.50")
