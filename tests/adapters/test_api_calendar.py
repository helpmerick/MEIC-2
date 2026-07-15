"""The /calendar/* HTTP surface -- CAL-01/03/04 mutations + the CAL-02/08 read
model, and the 2026-07-15 review's defence-in-depth input validation.

Everything these endpoints accept is journaled verbatim (the calendar store
is event-sourced) and a tag's label is later echoed into `blackout:<label>`
skip reasons (CAL-05), so malformed input is rejected 422 BEFORE journaling
-- never truncated/clamped (UI-03 precedent), never silently accepted.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from meic.adapters.api.app import create_app
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.calendar_store import CalendarStore
from meic.application.event_log import EventLog
from meic.application.persistent_state import PersistentState
from tests.harness.fake_clock import FastClock

NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def wired():
    events = EventLog(config_version="v1.71")
    store = CalendarStore(events, FastClock(NOW))
    state = PersistentState(InMemoryStateStore())
    app = create_app(state, events, calendar_store=store)
    return TestClient(app), events, store


@pytest.fixture
def client(wired):
    return wired[0]


# --- the happy paths -----------------------------------------------------------

def test_cal03_tag_untag_and_read_back(client):
    r = client.post("/calendar/tag", json={"day": "2026-07-15", "label": "FOMC"})
    assert r.status_code == 200 and r.json() == {"result": "tagged", "day": "2026-07-15",
                                                  "label": "FOMC"}
    body = client.get("/calendar").json()
    assert body["tags"]["2026-07-15"] == {"label": "FOMC", "origin": "manual", "category": None}

    assert client.delete("/calendar/tag/2026-07-15").status_code == 200
    assert client.get("/calendar").json()["tags"] == {}


def test_cal03_blank_label_defaults_to_the_day(client):
    r = client.post("/calendar/tag", json={"day": "2026-07-15"})
    assert r.status_code == 200 and r.json()["label"] == "2026-07-15"


def test_cal01_cal04_import_rule_and_staleness_read_back(client):
    r = client.post("/calendar/import", json={"category": "FOMC",
                                              "dates": ["2026-07-29", "2026-09-16"]})
    assert r.status_code == 200 and r.json()["count"] == 2
    assert client.post("/calendar/rule", json={"category": "FOMC"}).status_code == 200

    body = client.get("/calendar").json()
    assert body["tags"]["2026-07-29"]["origin"] == "auto"
    assert body["staleness"]["FOMC"]["stale"] is False
    assert body["staleness"]["FOMC"]["tier"] == 1
    assert "FOMC" in body["standing_rules"]

    assert client.delete("/calendar/rule/FOMC").status_code == 200
    assert client.get("/calendar").json()["tags"] == {}


def test_calendar_routes_400_when_no_store_is_wired():
    state = PersistentState(InMemoryStateStore())
    client = TestClient(create_app(state, EventLog()))
    assert client.get("/calendar").json() == {"available": False}
    assert client.post("/calendar/tag", json={"day": "2026-07-15"}).status_code == 400
    assert client.post("/calendar/rule", json={"category": "FOMC"}).status_code == 400


# --- CAL input validation (2026-07-15 review): reject 422, never journal --------

def _assert_nothing_journaled(events):
    assert list(events) == [], "a rejected request must never journal an event"


def test_tag_rejects_a_malformed_day(wired):
    client, events, _ = wired
    for bad in ("2026-7-15", "20260715", "next FOMC", "", "2026-07-15T10:00", "2026-07-15 ",
                # finding 4 (2026-07-15): right SHAPE, impossible DATE --
                # the date.fromisoformat round-trip is what catches these.
                "2026-13-45", "2026-02-30", "2026-00-01",
                # finding 4: Unicode decimal digits (Arabic-Indic/full-width)
                # match \d without re.ASCII -- must be rejected.
                "٢٠٢٦-07-15", "２０２６-07-15"):
        r = client.post("/calendar/tag", json={"day": bad, "label": "FOMC"})
        assert r.status_code == 422, repr(bad)
        assert r.json()["detail"]["reason"] in ("invalid_day",)
    _assert_nothing_journaled(events)


def test_untag_rejects_a_malformed_day(wired):
    client, events, _ = wired
    r = client.delete("/calendar/tag/not-a-day")
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "invalid_day"
    _assert_nothing_journaled(events)


def test_tag_rejects_a_newline_in_the_label(wired):
    """The review's named case: a label with a line break would smuggle a
    newline into the journal and into `blackout:<label>` skip reasons."""
    client, events, _ = wired
    r = client.post("/calendar/tag", json={"day": "2026-07-15", "label": "FOMC\nday"})
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "invalid_label"
    _assert_nothing_journaled(events)


def test_tag_rejects_control_characters_and_overlong_labels(wired):
    client, events, _ = wired
    for bad in ("FOMC\tday", "FOMC\rday", "FOMC\x1b[31m", "x" * 65):
        r = client.post("/calendar/tag", json={"day": "2026-07-15", "label": bad})
        assert r.status_code == 422, repr(bad)
        assert r.json()["detail"]["reason"] == "invalid_label"
    # 64 chars exactly is legal -- rejected means > the bound, never clamped at it.
    assert client.post("/calendar/tag", json={"day": "2026-07-15",
                                              "label": "x" * 64}).status_code == 200


def test_rule_rejects_an_unknown_category_and_a_bad_label(wired):
    client, events, _ = wired
    r = client.post("/calendar/rule", json={"category": "OPEX"})
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "unknown_category"
    r = client.post("/calendar/rule", json={"category": "FOMC", "label": "a\nb"})
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "invalid_label"
    r = client.delete("/calendar/rule/OPEX")
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "unknown_category"
    _assert_nothing_journaled(events)


def test_labels_must_be_strings_never_coerced(wired):
    """A JSON number/null label is rejected -- str(5)/str(None) journaling
    "5"/"None" would be a silent coercion, the fix-up class this refuses."""
    client, events, _ = wired
    r = client.post("/calendar/rule", json={"category": "FOMC", "label": 5})
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "invalid_label"
    r = client.post("/calendar/import", json={"category": "FOMC", "dates": ["2026-07-29"],
                                              "labels": {"2026-07-29": None}})
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "invalid_label"
    _assert_nothing_journaled(events)


def test_import_rejects_bad_dates_bad_labels_and_unknown_categories(wired):
    client, events, _ = wired
    r = client.post("/calendar/import", json={"category": "FOMC", "dates": ["july 29"]})
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "invalid_day"
    r = client.post("/calendar/import", json={"category": "FOMC", "dates": ["2026-07-29"],
                                              "labels": {"2026-07-29": "a\nb"}})
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "invalid_label"
    r = client.post("/calendar/import", json={"category": "QUAD_WITCHING",
                                              "dates": ["2026-09-18"]})
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "unknown_category"
    _assert_nothing_journaled(events)
