"""RPT-17/UI-33 -- GET /reports/day-table: the Trading tab's day-trades
table + the Timing & Unmanaged report. TC-RPT-23 binds against this endpoint
(and reporting/day_table.py's pure helpers directly). Uses the in-process
TestClient, same convention as tests/adapters/test_api.py /
test_api_reports.py.
"""
from datetime import date
from decimal import Decimal as D

from fastapi.testclient import TestClient

from meic.adapters.api.app import create_app
from meic.adapters.occ import occ_symbol
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.persistent_state import PersistentState
from meic.domain.events import (
    CondorFilled,
    DayArmed,
    DayBrokerConfirmed,
    EntryClosed,
    EntryMarkSample,
    FilledLeg,
    SettlementRecorded,
    SideExpired,
)

PANEL = "http://127.0.0.1"
EXP = date(2026, 7, 9)


def _client(events=None, *, entries_enricher=None):
    state = PersistentState(InMemoryStateStore())
    events = events if events is not None else []
    app = create_app(state, events, panel_origin=PANEL, entries_enricher=entries_enricher)
    return TestClient(app), state, events


def _leg(right, role, strike, qty=1, price=D("1.00")):
    return FilledLeg(symbol=occ_symbol("SPXW", EXP, right, D(strike)), right=right,
                      role=role, qty=qty, price=price)


FULL_LEGS = (
    _leg("P", "short", "7535"),
    _leg("P", "long", "7510"),
    _leg("C", "short", "7540"),
    _leg("C", "long", "7565"),
)


# --- Scenario 1: the table shows today's entries from the one aggregation path -

def test_day_table_shows_two_closed_and_one_open_entry_today():
    events = [
        DayArmed(date="2026-07-09", entry_count=3),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS,
                    at="2026-07-09T09:32:00+00:00", initiator="schedule",
                    target_premium=D("3.50")),
        SideExpired(entry_id="2026-07-09#1", side="PUT"),
        SideExpired(entry_id="2026-07-09#1", side="CALL"),
        CondorFilled(entry_id="2026-07-09#2", net_credit=D("3.40"), legs=FULL_LEGS,
                    at="2026-07-09T10:00:00+00:00", initiator="manual_entry"),
        EntryClosed(entry_id="2026-07-09#2", initiator="manual",
                    at="2026-07-09T11:00:00+00:00"),
        CondorFilled(entry_id="2026-07-09#3", net_credit=D("3.20"), legs=FULL_LEGS,
                    at="2026-07-09T11:30:00+00:00"),
    ]
    client, _state, _events = _client(events)

    body = client.get("/reports/day-table").json()

    assert body["date"] == "2026-07-09"
    ids = [r["entry_id"] for r in body["rows"]]
    assert ids == ["2026-07-09#1", "2026-07-09#2", "2026-07-09#3"]
    row1 = body["rows"][0]
    assert row1["side_badges"] == {"PUT": "expired", "CALL": "expired"}
    assert row1["net_credit"] == "360.00"
    assert row1["target_premium"] == "3.50"
    assert row1["initiator"] == "schedule"
    row2 = body["rows"][1]
    assert row2["initiator"] == "manual_entry"
    assert row2["closed_at"] == "2026-07-09T11:00:00+00:00"
    row3 = body["rows"][2]
    assert row3["status"] == "PROTECTED"
    assert row3["pnl_unrealized"] is True


def test_day_table_open_row_shows_live_pnl_badged_unrealized():
    events = [
        DayArmed(date="2026-07-09", entry_count=1),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS,
                    at="2026-07-09T09:32:00+00:00"),
    ]

    def enricher(cards):
        for c in cards:
            c["live_pnl"] = "123.45"
        return cards

    client, _state, _events = _client(events, entries_enricher=enricher)

    row = client.get("/reports/day-table").json()["rows"][0]
    assert row["pnl"] == "123.45"
    assert row["pnl_unrealized"] is True


def test_day_table_figures_match_the_canonical_aggregation_byte_for_byte():
    """RPT-09a: no view-local recompute -- the row's net_credit/pnl and the
    day-total's net_pnl/fees/total_credit must equal the SAME canonical
    folds.py functions applied directly to the raw events, not a re-derived
    number."""
    from meic.reporting.folds import core_results, entry_credit_dollars, entry_dollars
    from meic.reporting.periods import scope_events

    events = [
        DayArmed(date="2026-07-09", entry_count=1),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), fee=D("4.88"), legs=FULL_LEGS,
                    at="2026-07-09T09:32:00+00:00"),
        SideExpired(entry_id="2026-07-09#1", side="PUT"),
        SideExpired(entry_id="2026-07-09#1", side="CALL"),
    ]
    client, _state, _events = _client(events)
    body = client.get("/reports/day-table").json()

    from meic.domain.projection import fold
    e = fold(events).entries["2026-07-09#1"]
    row = body["rows"][0]
    assert row["net_credit"] == str(entry_credit_dollars(e))
    assert row["pnl"] == str(entry_dollars(e))

    totals = core_results(scope_events(events, ("2026-07-09",)))
    assert body["day_total"]["net_pnl"] == str(totals.net_pnl)
    assert body["day_total"]["fees"] == str(totals.fees)
    assert body["day_total"]["total_credit"] == str(totals.total_credit)


# --- Scenario 4: provisional stays provisional --------------------------------

def test_day_table_row_provisional_until_settlement_or_reconcile():
    events = [
        DayArmed(date="2026-07-09", entry_count=1),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS,
                    at="2026-07-09T09:32:00+00:00"),
        SideExpired(entry_id="2026-07-09#1", side="PUT"),
        SideExpired(entry_id="2026-07-09#1", side="CALL"),
    ]
    client, _state, _events = _client(events)
    row = client.get("/reports/day-table").json()["rows"][0]
    assert row["provisional"] is True


def test_day_table_row_not_provisional_once_settled():
    legs = FULL_LEGS
    events = [
        DayArmed(date="2026-07-09", entry_count=1),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=legs,
                    at="2026-07-09T09:32:00+00:00"),
        SideExpired(entry_id="2026-07-09#1", side="PUT"),
        SideExpired(entry_id="2026-07-09#1", side="CALL"),
        SettlementRecorded(entry_id="2026-07-09#1", day="2026-07-09",
                           at="2026-07-10T00:00:00+00:00", symbol=legs[0].symbol,
                           sub_type="Expiration", quantity=1, price=None, value=D("0"), fee=D("0")),
        SettlementRecorded(entry_id="2026-07-09#1", day="2026-07-09",
                           at="2026-07-10T00:00:00+00:00", symbol=legs[2].symbol,
                           sub_type="Expiration", quantity=1, price=None, value=D("0"), fee=D("0")),
    ]
    client, _state, _events = _client(events)
    row = client.get("/reports/day-table").json()["rows"][0]
    assert row["provisional"] is False


def test_day_table_row_not_provisional_once_broker_confirmed():
    events = [
        DayArmed(date="2026-07-09", entry_count=1),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS,
                    at="2026-07-09T09:32:00+00:00"),
        SideExpired(entry_id="2026-07-09#1", side="PUT"),
        SideExpired(entry_id="2026-07-09#1", side="CALL"),
        DayBrokerConfirmed(date="2026-07-09", at="2026-07-10T00:00:00+00:00"),
    ]
    client, _state, _events = _client(events)
    row = client.get("/reports/day-table").json()["rows"][0]
    assert row["provisional"] is False


# --- Scenario 2: Unmanaged P&L from recorded samples only ---------------------

def test_day_table_timing_unmanaged_row_shape_and_no_data_case():
    events = [
        DayArmed(date="2026-07-09", entry_count=1),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS,
                    at="2026-07-09T09:32:00+00:00"),
        EntryClosed(entry_id="2026-07-09#1", initiator="manual",
                    at="2026-07-09T14:00:00+00:00"),
    ]
    client, _state, _events = _client(events)
    row = client.get("/reports/day-table").json()["timing_unmanaged"][0]
    assert row["opened_at"] == "2026-07-09T09:32:00+00:00"
    assert row["closed_at"] == "2026-07-09T14:00:00+00:00"
    assert row["unmanaged_status"] == "no_data"
    assert row["unmanaged_pnl"] is None


def test_day_table_timing_unmanaged_row_computed_from_the_16_00_sample():
    events = [
        DayArmed(date="2026-07-09", entry_count=1),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS,
                    at="2026-07-09T09:32:00+00:00"),
        EntryClosed(entry_id="2026-07-09#1", initiator="manual",
                    at="2026-07-09T14:00:00+00:00"),
        EntryMarkSample(entry_id="2026-07-09#1", at="2026-07-09T20:00:00+00:00",
                        put_short_mid=D("0.50"), put_long_mid=D("0.05"),
                        call_short_mid=D("0.40"), call_long_mid=D("0.03")),
    ]
    client, _state, _events = _client(events)
    row = client.get("/reports/day-table").json()["timing_unmanaged"][0]
    assert row["unmanaged_status"] == "ok"
    assert D(row["unmanaged_pnl"]) == D("360.00") - D("82.00")


def test_day_table_no_data_for_the_day_returns_an_empty_shape():
    client, _state, _events = _client([])
    body = client.get("/reports/day-table").json()
    assert body == {"date": None, "mode": "paper", "rows": [], "day_total": None,
                    "timing_unmanaged": []}
