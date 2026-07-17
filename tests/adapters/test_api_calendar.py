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


# --- CAL-08: /state's today_blackout_label (slice 2, additive) ------------------

class _DayCommands:
    """Minimal `commands` double: the ONE method /state's banner field reads."""

    def __init__(self, day):
        self._day = day

    def day(self):
        return self._day


class _ExplodingCommands:
    def day(self):
        raise RuntimeError("day source exploded")


def _state_client(commands):
    events = EventLog(config_version="v1.71")
    store = CalendarStore(events, FastClock(NOW))
    state = PersistentState(InMemoryStateStore())
    app = create_app(state, events, commands=commands, calendar_store=store)
    return TestClient(app), store


def test_state_carries_todays_blackout_label(wired):
    """CAL-08: the trading panel's "Today: NO-TRADE — <label>" reads this
    field; tagged day -> the label, untagged -> None."""
    client, _, store = wired
    assert client.get("/state").json()["today_blackout_label"] is None  # untagged

    c2, store2 = _state_client(_DayCommands("2026-07-15"))
    store2.tag("2026-07-15", "FOMC")
    assert c2.get("/state").json()["today_blackout_label"] == "FOMC"


def test_state_blackout_label_is_none_without_a_calendar():
    """Pre-v1.71 wiring (no store): the field exists and is None -- a stable
    read-model shape, never a guess."""
    state = PersistentState(InMemoryStateStore())
    client = TestClient(create_app(state, EventLog()))
    assert client.get("/state").json()["today_blackout_label"] is None


def test_state_survives_a_broken_day_source_fail_open_never_500(caplog):
    """2026-07-15 review SHOULD-FIX, pinned: /state is the panel's most
    critical poll (armed/stop_trading/entries_enabled) -- a failure anywhere
    in the banner field's today+label computation must degrade to
    today_blackout_label None with EVERY other field intact and a 200, never
    a 500. Fail-open is never fail-silent: a log record lands (the same
    CAL-07 discipline calendar_store.label_for_day already applies)."""
    client, _ = _state_client(_ExplodingCommands())

    with caplog.at_level("ERROR", logger="meic.adapters.api.app"):
        r = client.get("/state")

    assert r.status_code == 200
    body = r.json()
    assert body["today_blackout_label"] is None
    # the critical fields are all present and typed as ever
    assert body["armed"] is False and body["stop_trading"] is False
    assert body["confirm_live"] is False and body["trading_mode"] == "paper"
    assert body["entries_enabled"] is False and body["blocking_state"] == "DISARMED"
    # fail-open, never fail-silent
    assert any("CAL-08 fail-open" in rec.message for rec in caplog.records)


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


# --- CAL-10 (v1.83): computed OpEx/quad-witch events on GET /calendar -----------

def test_cal10_get_calendar_carries_computed_events(client):
    body = client.get("/calendar").json()
    assert "2026-07-17" in body["computed_events"]["OPEX_MONTHLY"]
    assert "2026-09-18" in body["computed_events"]["QUAD_WITCH"]


def test_cal10_rule_endpoint_accepts_a_computed_category(client):
    """CAL-10: 'standing-rule capable exactly like fetched ones' -- the SAME
    /calendar/rule endpoint that already handles FOMC etc. must accept
    OPEX_MONTHLY/QUAD_WITCH too, and the resulting auto-tag shows up in the
    SAME read model, distinguishable from a fetched-category auto-tag only by
    its category name."""
    r = client.post("/calendar/rule", json={"category": "QUAD_WITCH"})
    assert r.status_code == 200
    body = client.get("/calendar").json()
    assert body["tags"]["2026-09-18"] == {"label": "QUAD_WITCH", "origin": "auto", "category": "QUAD_WITCH"}
    assert "2026-07-17" not in body["tags"]   # monthly OpEx, no rule for it -- CAL-10: never auto-blocked

    assert client.delete("/calendar/rule/QUAD_WITCH").status_code == 200
    assert "2026-09-18" not in client.get("/calendar").json()["tags"]


def test_cal10_import_still_rejects_a_computed_category_never_fetched(client, events=None):
    r = client.post("/calendar/import", json={"category": "OPEX_MONTHLY", "dates": ["2026-07-17"]})
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "unknown_category"


def test_cal10_computed_categories_never_appear_in_staleness(client):
    body = client.get("/calendar").json()
    assert "OPEX_MONTHLY" not in body["staleness"]
    assert "QUAD_WITCH" not in body["staleness"]


# --- CAL-11 (v1.84): event proximity warnings -----------------------------------

def test_cal11_warnings_endpoint_returns_day_of_and_lead_tiers(wired):
    """`wired`'s create_app call passes no explicit event_warning_lead_days,
    so it exercises the doc-06 DEFAULT (3) end-to-end."""
    client, _, _ = wired
    r = client.post("/calendar/import", json={"category": "FOMC",
                                              "dates": ["2026-07-15", "2026-07-16",
                                                        "2026-07-17", "2026-07-20"]})
    assert r.status_code == 200

    body = client.get("/calendar/warnings").json()
    assert body["available"] is True
    fomc = [w for w in body["warnings"] if w["category"] == "FOMC"]
    tiers = {w["proximity_tier"] for w in fomc}
    assert tiers == {0, 1, 2, 3}
    today = next(w for w in fomc if w["proximity_tier"] == 0)
    assert today["human_label"] == "Today is FOMC"
    t2 = next(w for w in fomc if w["proximity_tier"] == 2)
    assert t2["human_label"] == "FOMC in 2 trading days (Fri)"
    # nearest-first ordering (rule 4)
    all_tiers = [w["proximity_tier"] for w in body["warnings"]]
    assert all_tiers == sorted(all_tiers)


def test_cal11_warnings_unavailable_without_a_wired_store():
    state = PersistentState(InMemoryStateStore())
    client = TestClient(create_app(state, EventLog()))
    assert client.get("/calendar/warnings").json() == {"available": False, "warnings": []}
    assert client.post("/calendar/warnings/dismiss",
                        json={"category": "FOMC", "event_date": "2026-07-15", "tier": 0}
                        ).status_code == 400


def test_cal11_dismiss_removes_only_that_tier_and_survives_a_rebuilt_store(wired):
    client, events, store = wired
    client.post("/calendar/import", json={"category": "FOMC",
                                          "dates": ["2026-07-15", "2026-07-16",
                                                    "2026-07-17", "2026-07-20"]})
    r = client.post("/calendar/warnings/dismiss",
                     json={"category": "FOMC", "event_date": "2026-07-20", "tier": 3})
    assert r.status_code == 200 and r.json()["result"] == "dismissed"

    body = client.get("/calendar/warnings").json()
    fomc_tiers = {w["proximity_tier"] for w in body["warnings"] if w["category"] == "FOMC"}
    assert fomc_tiers == {0, 1, 2}   # T-3 gone, T-2/T-1/T-0 still present

    # REC-07: rebuild a brand-new app/store over the SAME journal ("restart").
    restarted_store = CalendarStore(list(events), FastClock(NOW))
    restarted_state = PersistentState(InMemoryStateStore())
    restarted = TestClient(create_app(restarted_state, list(events), calendar_store=restarted_store))
    restarted_body = restarted.get("/calendar/warnings").json()
    restarted_tiers = {w["proximity_tier"] for w in restarted_body["warnings"] if w["category"] == "FOMC"}
    assert restarted_tiers == {0, 1, 2}   # never re-nags


def test_cal11_dismiss_rejects_a_malformed_day_and_an_unknown_category(wired):
    client, events, _ = wired
    r = client.post("/calendar/warnings/dismiss",
                     json={"category": "FOMC", "event_date": "not-a-day", "tier": 0})
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "invalid_day"

    r = client.post("/calendar/warnings/dismiss",
                     json={"category": "NOT_REAL", "event_date": "2026-07-15", "tier": 0})
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "unknown_category"
    _assert_nothing_journaled(events)


def test_cal11_a_tier2_fed_speaker_warning_is_marked_best_effort_over_http(wired):
    client, _, _ = wired
    client.post("/calendar/import", json={"category": "FED_SPEAKER", "dates": ["2026-07-16"]})
    body = client.get("/calendar/warnings").json()
    speaker = [w for w in body["warnings"] if w["category"] == "FED_SPEAKER"]
    assert len(speaker) == 1
    assert speaker[0]["best_effort"] is True and speaker[0]["tier"] == 2


def test_cal11_untagged_computed_opex_day_warns_but_never_blocks(wired):
    """TC-CAL-05 scenario 4 over HTTP: the warning feed surfaces the computed
    OpEx day (2026-07-17, within default lead_days=3 of NOW=2026-07-15), but
    with NO standing rule the SAME day reads untagged on the CAL-05 read model."""
    client, _, store = wired
    body = client.get("/calendar/warnings").json()
    opex = [w for w in body["warnings"] if w["category"] == "OPEX_MONTHLY" and w["event_date"] == "2026-07-17"]
    assert len(opex) == 1 and opex[0]["computed"] is True
    assert client.get("/calendar").json()["tags"].get("2026-07-17") is None
    assert store.label_for_day("2026-07-17") is None
