"""TC-CAL-04 -- CAL-10 computed OpEx/quad-witch events (doc 11, v1.83).

Binds all three scenarios against the BACKEND halves: the pure calendar math
(application/opex_calendar.py), domain/trading_calendar.py's
COMPUTED_CATEGORIES/effective_tags/label_for_day extension, and
application/calendar_store.py's operator-facing surface (set_standing_rule,
label_for_day, staleness_report, import_events). The "badged distinctly" UI
half (a distinct marker/class for QUAD_WITCH vs OPEX_MONTHLY) is separately
covered by the frontend companion's CalendarPage.test.tsx -- not duplicated
here, same "prove the gherkin, not just the unit, honestly only for the half
this file actually owns" discipline test_tc_cal_03.py's own docstring states.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pytest_bdd import given, scenarios, then

from meic.application.calendar_store import CalendarStore, UnknownCalendarCategory
from meic.application.event_log import EventLog
from meic.application.opex_calendar import _third_friday, opex_events_for_year
from meic.application.nyse_holidays import nyse_holidays
from meic.application.market_calendar import is_trading_day
from meic.domain.trading_calendar import COMPUTED_CATEGORIES
from tests.harness.fake_clock import FakeClock

scenarios("../features/TC-CAL-04.feature")

NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


def _fresh_store(start: datetime = NOW):
    events = EventLog(config_version="v1.83")
    clock = FakeClock(start)
    return events, clock, CalendarStore(events, clock)


@pytest.fixture
def world():
    return {}


# --- Scenario 1: Monthly and quarterly OpEx compute correctly ---------------

@then("2026-07-17 is an OPEX_MONTHLY event (third Friday of July 2026)")
def _(world):
    events = opex_events_for_year(2026)
    assert events["2026-07-17"] == "OPEX_MONTHLY"


@then("2026-09-18 is a QUAD_WITCH event, badged distinctly from monthly OpEx")
def _(world):
    events = opex_events_for_year(2026)
    assert events["2026-09-18"] == "QUAD_WITCH"
    assert events["2026-09-18"] != events["2026-07-17"]  # distinct category


@then("no weekly or daily expiration ever renders as a calendar event")
def _(world):
    events = opex_events_for_year(2026)
    # exactly one computed event per month -- no weekly/daily 0DTE noise.
    assert len(events) == 12
    assert set(events.values()) <= {"OPEX_MONTHLY", "QUAD_WITCH"}


# --- Scenario 2: A holiday third Friday shifts to the preceding trading day -

@given("a month whose third Friday is an exchange holiday per the DAY-01a calendar (real vector: April 2000, Good Friday the 21st)")
def _(world):
    holidays_2000 = nyse_holidays(2000)
    world["holidays_2000"] = holidays_2000
    assert date(2000, 4, 21) in holidays_2000                # Good Friday
    assert _third_friday(2000, 4) == date(2000, 4, 21)        # AND the 3rd Friday
    assert not is_trading_day(date(2000, 4, 21), holidays=holidays_2000)
    world["events_2000"] = opex_events_for_year(2000)


@then("the OpEx event lands on the preceding trading day, never on the holiday")
def _(world):
    events_2000 = world["events_2000"]
    assert "2000-04-20" in events_2000    # preceding trading day (a Thursday)
    assert "2000-04-21" not in events_2000   # never on the holiday itself


# --- Scenario 3: Computed events are taggable but never auto-blocked/stale --

@given('a standing rule "always block QUAD_WITCH"')
def _(world):
    events, clock, store = _fresh_store()
    store.set_standing_rule("QUAD_WITCH")
    world.update(events=events, clock=clock, store=store)


@then("quad-witch days auto-tag while monthly OpEx days stay untagged and tradeable")
def _(world):
    store = world["store"]
    assert store.label_for_day("2026-09-18") == "QUAD_WITCH"   # auto-tagged
    assert store.label_for_day("2026-07-17") is None            # untagged, no rule for it


@then("computed events carry no staleness banner and trigger no fetch")
def _(world):
    store = world["store"]
    stale = store.staleness_report(stale_after_days=45)
    assert "OPEX_MONTHLY" not in stale
    assert "QUAD_WITCH" not in stale   # never imported, so never staleness-tracked

    # structural proof of "trigger no fetch": import_events must keep
    # rejecting computed categories -- they have no fetch/source domain.
    with pytest.raises(UnknownCalendarCategory):
        store.import_events(category="OPEX_MONTHLY", dates=["2026-07-17"])
    with pytest.raises(UnknownCalendarCategory):
        store.import_events(category="QUAD_WITCH", dates=["2026-09-18"])

    # COMPUTED_CATEGORIES is deliberately disjoint from the fetchable set.
    from meic.domain.trading_calendar import KNOWN_CATEGORIES
    assert COMPUTED_CATEGORIES.isdisjoint(KNOWN_CATEGORIES)
