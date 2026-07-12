"""Hand-written step definitions for TC-UI-06 — the next-entry countdown
proves the schedule is actively watched (UI-24).

Two halves, bound two different ways:

  * BACKEND: /day/status's `seconds_to_next` (server.py's `_day_status_extras`,
    fed `_remaining_rows`' output) is derived from the `now` it is PASSED, never
    a live wall clock -- proven directly in Python by calling it with two
    different `now` values and observing `seconds_to_next` move accordingly
    (exactly as tests/application/test_day_supervisor.py's
    TestDayStatusExtras does; that file is owned by another concurrent change
    and is not touched here).

  * FRONTEND: the ticking display and the idle/no-more-entries states live in
    frontend/src/components/NextEntryCountdown.tsx, covered by
    NextEntryCountdown.test.tsx. Python cannot execute that TSX directly, so
    (same strategy as TC-UI-05) this shells out to the REAL vitest suite via
    the session-scoped `vitest_result` fixture in tests/bdd/conftest.py --
    shared with TC-UI-05 so the vitest/esbuild startup cost is paid once for
    the whole tests/bdd run, not once per scenario.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pytest_bdd import given, scenarios, then

from meic.adapters.api.server import _day_status_extras
from meic.composition.live_runtime import ScheduledRow

scenarios("../features/TC-UI-06.feature")

NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def world():
    return {}


@given("the bot is ARMED with a next entry composed")
def _(world):
    # One future row, 90 seconds out -- mirrors test_day_supervisor.py's
    # TestDayStatusExtras fixtures.
    world["rows"] = [ScheduledRow(NOW + timedelta(seconds=90), number=1)]


@then("the panel shows the entry's ET time and a ticking countdown")
def _(world, vitest_result):
    extras = _day_status_extras(world["rows"], NOW)
    assert extras["next_entry_at"] == world["rows"][0].when.isoformat()
    assert extras["seconds_to_next"] == 90

    rc, output = vitest_result
    assert rc == 0, output
    assert "shows the next entry's ET time and a ticking countdown when armed" in output


@then("the value derives from the backend's seconds_to_next, never the browser clock")
def _(world, vitest_result):
    # Same rows, two DIFFERENT `now`s 30 seconds apart -> seconds_to_next moves
    # by exactly that much. There is no wall clock in `_day_status_extras` at
    # all: it is a pure function of the `now` argument it is passed.
    later = NOW + timedelta(seconds=30)
    first = _day_status_extras(world["rows"], NOW)
    second = _day_status_extras(world["rows"], later)
    assert first["seconds_to_next"] - second["seconds_to_next"] == 30

    rc, output = vitest_result
    assert rc == 0, output
    # The frontend renders exactly the backend-supplied seconds_to_next (125 ->
    # "in 2:05"), never a value it derived itself from Date.now().
    assert "shows the next entry's ET time and a ticking countdown when armed" in output


@then('DISARMED shows "schedule idle" and an exhausted schedule shows "no more entries today"')
def _(world, vitest_result):
    # Backend half of "exhausted": no remaining rows -> no next entry.
    exhausted = _day_status_extras([], NOW)
    assert exhausted == {"next_entry_at": None, "seconds_to_next": None, "entries_remaining": 0}

    # Frontend half: DISARMED ("schedule idle — arm to run") and an armed-but-
    # exhausted schedule ("no more entries today") are two distinct messages.
    rc, output = vitest_result
    assert rc == 0, output
    assert 'shows "schedule idle — arm to run" when disarmed, with no countdown' in output
    assert 'shows "no more entries today" when armed with nothing left' in output
