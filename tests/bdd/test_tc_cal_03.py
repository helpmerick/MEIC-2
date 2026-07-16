"""TC-CAL-03 -- CAL-09 daily official-source auto-refresh (doc 11, v1.77).

Binds all four scenarios against `CalendarRefreshCoordinator` (application/
calendar_refresh.py) driven by fake `CalendarSource`s -- never a real
network call (CAL-09's own domain-allowlist/parse/plausibility machinery is
unit-tested directly in tests/adapters/test_calendar_sources.py and
tests/application/test_calendar_refresh.py; this file only proves the four
RATIFIED scenarios read correctly end-to-end through the real CalendarStore/
domain fold, the same "prove the gherkin, not just the unit" split every
other TC-CAL-*.py file in this repo uses).

No frontend clause appears in any of these four scenarios (unlike TC-CAL-01/
02, which have an explicit "OK dialog ..." / UI-visible clause bound via
`vitest_cal_result`) -- CAL-09's own UI surface (a DISPUTED marker in the
year view) is UI-30/CAL-08 territory, a separate (not yet built) slice; this
file is backend-only, honestly, because the ratified text itself is."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from pytest_bdd import given, scenarios, then

from meic.application.calendar_refresh import CalendarRefreshCoordinator, CategoryFetch
from meic.application.calendar_store import CalendarStore
from meic.domain.events import CalendarRefreshRejected, CalendarRefreshSucceeded

scenarios("../features/TC-CAL-03.feature")

UTC = timezone.utc
NOW = datetime(2026, 7, 16, 9, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        self._now = now


class _FakeAlerts:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def alert(self, level: str, message: str, **context) -> None:
        self.calls.append((level, message, context))


class _ScriptedSource:
    """A CalendarSource stand-in whose fetch() replays one canned result
    per call (never a real network call)."""

    def __init__(self, categories, script) -> None:
        self.categories = categories
        self._script = list(script)

    async def fetch(self):
        return self._script.pop(0) if self._script else []


@pytest.fixture
def world():
    events: list = []
    clock = _Clock(NOW)
    store = CalendarStore(events, clock)
    alerts = _FakeAlerts()
    return {"events": events, "clock": clock, "store": store, "alerts": alerts}


# --- Scenario 1: A successful refresh appends and auto-tags ------------------

@given("a daily fetch returning next year's FOMC schedule")
def _(world):
    world["store"].set_standing_rule("FOMC")  # "a standing 'always block FOMC' rule"
    source = _ScriptedSource(("FOMC",), [[CategoryFetch(
        category="FOMC", ok=True, dates=("2027-01-27", "2027-03-17"),
        url="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")]])
    coord = CalendarRefreshCoordinator(sources=(source,), store=world["store"],
                                       clock=world["clock"], alerts=world["alerts"],
                                       fail_alert_days=3)
    asyncio.run(coord.run_once(NOW))


@then("new events append with source and timestamp journaled")
def _(world):
    succeeded = [e for e in world["events"] if isinstance(e, CalendarRefreshSucceeded)]
    assert len(succeeded) == 1
    ev = succeeded[0]
    assert ev.dates == ("2027-01-27", "2027-03-17")
    assert ev.source == "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
    assert ev.fetched_at  # journaled, non-empty


@then('a standing "always block FOMC" rule auto-tags the new dates')
def _(world):
    store = world["store"]
    assert store.label_for_day("2027-01-27") == "FOMC"
    assert store.label_for_day("2027-03-17") == "FOMC"


# --- Scenario 2: A garbage fetch can never damage existing data -------------

@given("a fetch that fails, parses empty, or returns 40 FOMC dates")
def _(world):
    store, alerts, clock = world["store"], world["alerts"], world["clock"]
    good = _ScriptedSource(("FOMC",), [[CategoryFetch(
        category="FOMC", ok=True, dates=("2026-09-16",), url="u")]])
    asyncio.run(CalendarRefreshCoordinator(sources=(good,), store=store, clock=clock,
                                           alerts=alerts, fail_alert_days=3).run_once(NOW))
    world["events_before"] = list(world["events"])

    garbage = _ScriptedSource(("FOMC",), [[CategoryFetch(
        category="FOMC", ok=False, reason="implausible_count:40", url="u")]])
    clock.set(datetime(2026, 7, 17, 9, 0, tzinfo=UTC))
    coord = CalendarRefreshCoordinator(sources=(garbage,), store=store, clock=clock,
                                       alerts=alerts, fail_alert_days=3)
    asyncio.run(coord.run_once(clock.now()))


@then("it is rejected whole, existing events are byte-identical, and one alert fires")
def _(world):
    store = world["store"]
    assert store.state().imports["FOMC"].dates == {"2026-09-16"}  # untouched
    before_success_events = [e for e in world["events_before"]
                             if isinstance(e, CalendarRefreshSucceeded)]
    after_success_events = [e for e in world["events"] if isinstance(e, CalendarRefreshSucceeded)]
    assert after_success_events == before_success_events  # byte-identical
    rejected = [e for e in world["events"] if isinstance(e, CalendarRefreshRejected)]
    assert len(rejected) == 1 and rejected[0].reason == "implausible_count:40"
    reject_alerts = [c for c in world["alerts"].calls if "REJECTED" in c[1]]
    assert len(reject_alerts) == 1


# --- Scenario 3: A vanished date is disputed, never dropped -----------------

@given("a previously imported FOMC date absent from today's fetch")
def _(world):
    store, alerts, clock = world["store"], world["alerts"], world["clock"]
    first = _ScriptedSource(("FOMC",), [[CategoryFetch(
        category="FOMC", ok=True, dates=("2026-09-16", "2026-10-28"), url="u")]])
    asyncio.run(CalendarRefreshCoordinator(sources=(first,), store=store, clock=clock,
                                           alerts=alerts, fail_alert_days=3).run_once(NOW))
    store.tag("2026-10-28", "FOMC")  # the operator's own NO-TRADE tag on the day at risk

    clock.set(datetime(2026, 7, 17, 9, 0, tzinfo=UTC))
    second = _ScriptedSource(("FOMC",), [[CategoryFetch(
        category="FOMC", ok=True, dates=("2026-09-16",), url="u")]])  # 2026-10-28 vanished
    asyncio.run(CalendarRefreshCoordinator(sources=(second,), store=store, clock=clock,
                                           alerts=alerts, fail_alert_days=3).run_once(clock.now()))


@then("the event is marked DISPUTED with an alert and its NO-TRADE tag stands")
def _(world):
    store, alerts = world["store"], world["alerts"]
    imp = store.state().imports["FOMC"]
    assert "2026-10-28" in imp.dates      # never dropped
    assert "2026-10-28" in imp.disputed   # marked DISPUTED
    assert store.label_for_day("2026-10-28") == "FOMC"  # its NO-TRADE tag stands
    disputed_alerts = [c for c in alerts.calls if "DISPUTED" in c[1]]
    assert len(disputed_alerts) == 1


# --- Scenario 4: Feed failure is loud but never blocks trading -------------

@given("cal_refresh_fail_alert_days consecutive failures")
def _(world):
    store, alerts, clock = world["store"], world["alerts"], world["clock"]
    fail_alert_days = 3
    bad = lambda: _ScriptedSource(("FOMC",), [[CategoryFetch(
        category="FOMC", ok=False, reason="fetch_failed:timeout", url="u")]])
    for day_offset in range(fail_alert_days):
        clock.set(datetime(2026, 7, 16 + day_offset, 9, 0, tzinfo=UTC))
        coord = CalendarRefreshCoordinator(sources=(bad(),), store=store, clock=clock,
                                           alerts=alerts, fail_alert_days=fail_alert_days)
        asyncio.run(coord.run_once(clock.now()))
    world["fail_alert_days"] = fail_alert_days


@then("a persistent alert raises and entries remain ungated by the calendar's absence")
def _(world):
    alerts, store = world["alerts"], world["store"]
    critical = [c for c in alerts.calls if c[0] == "critical"]
    assert len(critical) == 1
    assert f"{world['fail_alert_days']} consecutive day" in critical[0][1]
    # CAL-07 fail-open, untouched by CAL-09: a category with no successful
    # import at all still reads as untagged, never as a block.
    assert store.label_for_day("2026-07-20") is None
