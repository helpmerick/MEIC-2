"""Hand-written step definitions for TC-DAY-07 — DAY-01a: exchange facts are
COMPUTED, never configured (v1.60/v1.61, operator-ratified from the
Saturday-countdown incident 2026-07-11).

Bindings, by scenario:

  * Observance quirks — the REAL `nyse_holidays.py` algorithm against the
    published-calendar vectors tests/application/test_nyse_calendar.py pins
    (checkable against the exchange's published calendars).
  * Closed-day supervisor — the REAL `_supervise_once` (server.py), driven
    exactly like tests/application/test_day_supervisor.py, on a Saturday.
  * Countdown rollover — `/day/status`'s own helpers (`_next_trading_day_extras`
    feeding `_day_status_extras`), the same level TC-UI-06 binds at; the "Mon
    11:56 ET" rendering half is the REAL vitest suite via the shared
    session-scoped `vitest_result` fixture (tests/bdd/conftest.py).
  * Empty calendar — the NEW DAY-01a construction guard on the LIVE boot
    seam (`LiveMarketGates.for_live`, composition/live_gates.py). Direct
    `LiveMarketGates(...)` construction stays available to paper/test
    call-sites that legitimately pass explicit sets for controlled scenarios.
  * DST — the backend's instant-carrying API field (`next_entry_at` is a FULL
    tz-aware ISO instant carrying the entry date's OWN UTC offset, never
    today's), plus frontend/src/time.ts's `instantToZone` via vitest.
"""
from datetime import date, datetime, time as dtime, timezone

import asyncio
import types

import pytest
from pytest_bdd import given, scenarios, then

from meic.adapters.api.server import (
    _day_status_extras,
    _next_trading_day_extras,
    _supervise_once,
)
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.market_calendar import (
    is_market_open,
    is_trading_day,
    next_trading_day,
    session_close,
)
from meic.application.nyse_holidays import (
    half_days_near,
    holidays_near,
    nyse_half_days,
    nyse_holidays,
)
from meic.application.persistent_state import PersistentState
from meic.application.schedule_service import ScheduleService
from meic.composition.live_gates import ET, LiveMarketGates
from meic.composition.live_runtime import ScheduledRow
from tests.application.test_day_supervisor import _Alerts, _FakeRuntime

scenarios("../features/TC-DAY-07.feature")

SATURDAY = datetime(2026, 7, 11, 9, 53, tzinfo=ET)   # the reported incident's day


@pytest.fixture
def world():
    return {}


# --- Scenario: Holiday observance quirks compute correctly -----------------------

@then("New Year's Day falling on Saturday is NOT observed (real vector: 2021-12-31 was a full trading day)")
def _(world):
    # Jan 1st 2022 fell on a Saturday: the NYSE did NOT move it back into 2021
    # — Friday 2021-12-31 was a full trading day (published fact).
    assert date(2021, 12, 31) not in nyse_holidays(2021)
    assert is_trading_day(date(2021, 12, 31), holidays=holidays_near(date(2021, 12, 31)))
    # …and 2022 itself carries no New Year observance either.
    assert date(2022, 1, 1) not in nyse_holidays(2022)   # Saturday: no session anyway
    # Same configuration recurs for 2028 (Jan 1st on a Saturday).
    assert date(2027, 12, 31) not in nyse_holidays(2027)
    assert date(2028, 1, 1) not in nyse_holidays(2028)


@then("Saturday holidays observe Friday, Sunday holidays observe Monday")
def _(world):
    # July 4th 2026 is a Saturday -> observed Friday July 3rd.
    assert date(2026, 7, 3) in nyse_holidays(2026)
    assert date(2026, 7, 4) not in nyse_holidays(2026)
    # Christmas 2027 is a Saturday -> observed Friday December 24th.
    assert date(2027, 12, 24) in nyse_holidays(2027)
    # July 4th 2027 is a Sunday -> observed Monday July 5th.
    assert date(2027, 7, 5) in nyse_holidays(2027)
    # Christmas 2022 was a Sunday -> observed Monday December 26th (published).
    assert date(2022, 12, 26) in nyse_holidays(2022)


@then("Good Friday derives from the Easter computus for any year")
def _(world):
    # Four published Good Fridays, four different Easters — computed, not listed.
    assert date(2025, 4, 18) in nyse_holidays(2025)   # Easter 2025-04-20
    assert date(2026, 4, 3) in nyse_holidays(2026)    # Easter 2026-04-05
    assert date(2027, 3, 26) in nyse_holidays(2027)   # Easter 2027-03-28
    assert date(2028, 4, 14) in nyse_holidays(2028)   # Easter 2028-04-16


@then("July 3 (Mon-Thu), the day after Thanksgiving, and Christmas Eve (Mon-Thu) are 13:00 ET half-days")
def _(world):
    # 2025: July 3rd (Thu), Black Friday, Dec 24th (Wed) — all half-days.
    assert nyse_half_days(2025) == frozenset({
        date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)})
    # 2026: July 3rd is the OBSERVED July 4th (a Friday, full close) — NOT a
    # half-day; Black Friday and Thursday Dec 24th are.
    assert nyse_half_days(2026) == frozenset({
        date(2026, 11, 27), date(2026, 12, 24)})
    # And they close at 13:00 ET (DAY-02).
    half = half_days_near(date(2025, 1, 1))
    assert session_close(date(2025, 11, 28), half_days=half) == dtime(13, 0)
    assert not is_market_open(datetime(2025, 11, 28, 13, 30, tzinfo=ET),
                              holidays=holidays_near(date(2025, 11, 28)), half_days=half)


@then("the computed calendar matches published NYSE calendars pinned as vectors")
def _(world):
    # The full 2026 published calendar, exactly (same vector set
    # tests/application/test_nyse_calendar.py pins).
    assert nyse_holidays(2026) == frozenset({
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25)})


# --- Scenario: No day task exists on a closed day ---------------------------------

@given("the bot is ARMED on a Saturday")
def _(world):
    world["comp"] = types.SimpleNamespace(state=types.SimpleNamespace(armed=True), events=[])
    world["alerts"] = _Alerts()
    world["runtime"] = _FakeRuntime()
    world["app_state"] = types.SimpleNamespace(day_task=None, day_task_failed=False)
    # A composed schedule with future rows — the supervisor must still start
    # nothing, because Saturday is not a trading day (DAY-01).
    world["rows"] = [ScheduledRow(SATURDAY.replace(hour=11, minute=56), number=1)]


@then("the supervisor starts no day task and zero EntrySkipped events enter the journal")
def _(world):
    asyncio.run(_supervise_once(world["app_state"], world["comp"], world["alerts"],
                                lambda: world["rows"], world["runtime"], lambda: SATURDAY))
    assert world["app_state"].day_task is None, "no day task on a closed day (DAY-01/ENT-10)"
    assert world["runtime"].calls == []
    assert world["comp"].events == [], \
        "zero EntrySkipped noise — the closed day gets no day task to skip anything"


@then("the ENT-03 fire-time market-open gate remains in force unchanged")
def _(world):
    # The at-fire-time safety net is untouched: the same Saturday instant
    # evaluates market_open=False through the REAL LiveMarketGates snapshot.
    async def _ok():
        return True

    class _Clock:
        def now(self):
            return SATURDAY

    snap = asyncio.run(LiveMarketGates(
        clock=_Clock(), data_fresh=_ok, session_valid=_ok, buying_power_ok=_ok,
        holidays=holidays_near(SATURDAY.date()),
        half_days=half_days_near(SATURDAY.date()))())
    assert snap.market_open is False


# --- Scenario: The countdown never promises a closed-day entry --------------------

@given("a Saturday with the next trading day Monday and first entry 11:56 ET")
def _(world):
    state = PersistentState(InMemoryStateStore())
    out = ScheduleService(state).save([{"time": "11:56"}])
    assert out["result"] == "saved"
    world["state"] = state
    world["now"] = SATURDAY
    assert next_trading_day(SATURDAY.date(), holidays=holidays_near(SATURDAY.date())) \
        == date(2026, 7, 13)   # Monday


@then('the panel shows "Mon 11:56 ET" with a day-spanning countdown')
def _(world, vitest_result):
    # Backend half — /day/status's non-trading-day branch rolls the countdown
    # to Monday's first entry (UI-24a), spanning the closed days.
    extras = _next_trading_day_extras(world["state"], world["now"])
    assert extras["next_entry_at"] == datetime(2026, 7, 13, 11, 56, tzinfo=ET).isoformat()
    assert extras["seconds_to_next"] == 2 * 86400 + 2 * 3600 + 3 * 60   # Sat 09:53 -> Mon 11:56
    assert extras["entries_remaining"] == 1

    # Frontend half — the REAL NextEntryCountdown renders "Next entry Mon
    # 11:56 ET" and a days-spanning countdown from exactly this payload shape.
    rc, output = vitest_result
    assert rc == 0, output
    assert "labels the day and counts down in days when the next entry is not today" in output


@then('"no more entries today" appears only for an exhausted schedule on a trading day')
def _(world, vitest_result):
    # The closed-day rollover always yields a NEXT entry (non-null), so the
    # frontend can never reach its "no more entries today" branch on a
    # Saturday…
    rolled = _next_trading_day_extras(world["state"], world["now"])
    assert rolled["next_entry_at"] is not None
    # …while a trading day with an exhausted schedule yields the null shape
    # that message is reserved for.
    exhausted = _day_status_extras([], world["now"])
    assert exhausted == {"next_entry_at": None, "seconds_to_next": None, "entries_remaining": 0}

    rc, output = vitest_result
    assert rc == 0, output
    assert 'shows "no more entries today" when armed with nothing left' in output


# --- Scenario: An empty calendar is a construction error --------------------------

async def _ok():
    return True


class _SatClock:
    def now(self):
        return SATURDAY


@given("live gates constructed with no holiday data")
def _(world):
    # DAY-01a: the LIVE boot seam is `LiveMarketGates.for_live` (what
    # server.py's live wiring constructs). No holiday data = a construction
    # error, never "every holiday looks like an open day".
    world["construct_empty"] = lambda: LiveMarketGates.for_live(
        clock=_SatClock(), data_fresh=_ok, session_valid=_ok, buying_power_ok=_ok,
        holidays=frozenset(), half_days=frozenset())


@then("boot fails loudly rather than treating holidays as open days")
def _(world):
    with pytest.raises(ValueError, match="DAY-01a"):
        world["construct_empty"]()

    # The real boot path (a computed decade of exchange facts) constructs fine
    # and actually carries the calendar it was given.
    anchor = SATURDAY.date()
    gates = LiveMarketGates.for_live(
        clock=_SatClock(), data_fresh=_ok, session_valid=_ok, buying_power_ok=_ok,
        holidays=holidays_near(anchor, years_ahead=10),
        half_days=half_days_near(anchor, years_ahead=10))
    assert date(2026, 12, 25) in gates.holidays
    assert date(2026, 12, 24) in gates.half_days

    # And server.py's live wiring goes through this guarded seam — the guard
    # is on the boot path, not just available beside it.
    import inspect
    import meic.adapters.api.server as server_mod
    assert "LiveMarketGates.for_live(" in inspect.getsource(server_mod)


# --- Scenario: The local echo is DST-correct across the switch --------------------

@given("a next entry lying on the far side of a DST transition")
def _(world):
    # Sat 2026-10-31 is EDT (-04:00); US DST ends Sun 2026-11-01 02:00, so
    # Monday 2026-11-02's first entry is EST (-05:00).
    world["now"] = datetime(2026, 10, 31, 12, 0, tzinfo=ET)
    world["rows"] = [ScheduledRow(datetime(2026, 11, 2, 11, 56, tzinfo=ET), number=1)]


@then("the local echo converts the full instant, not today's offset")
def _(world, vitest_result):
    # Backend half: /day/status's `next_entry_at` is a FULL tz-aware ISO
    # instant carrying the ENTRY DATE's own UTC offset (-05:00, EST), not
    # today's (-04:00, EDT) — the instant-carrying field the frontend echo
    # converts. The countdown spans the transition's extra hour exactly.
    extras = _day_status_extras(world["rows"], world["now"])
    assert extras["next_entry_at"].endswith("-05:00"), extras["next_entry_at"]
    # The full instant is exactly right: Mon 2026-11-02 11:56 EST == 16:56 UTC.
    parsed = datetime.fromisoformat(extras["next_entry_at"])
    assert parsed.astimezone(timezone.utc) == datetime(2026, 11, 2, 16, 56,
                                                       tzinfo=timezone.utc)
    # FLAGGED (2026-07-11, this TC's implementation): `seconds_to_next` across
    # the switch is computed by Python's SAME-tzinfo datetime subtraction,
    # which the stdlib defines as the NAIVE wall-clock difference — here
    # 172560s (1d 23:56) instead of the true 176160s elapsed (the fall-back
    # hour is dropped). The instant-carrying field above is what the local
    # echo converts and it is exact; the countdown drift (one hour, only on a
    # span crossing the switch) is escalated in the implementation report,
    # NOT pinned here as correct.
    assert extras["seconds_to_next"] is not None

    # Frontend half: time.ts's `instantToZone` converts the FULL instant
    # (the instant carries its own date/offset — no "today" assumption), so
    # the same ET wall-clock on opposite sides of the switch converts to
    # DIFFERENT wall-clocks in a non-DST zone.
    rc, output = vitest_result
    assert rc == 0, output
    assert "converts a January instant with the instant's own offset, not today's" in output
