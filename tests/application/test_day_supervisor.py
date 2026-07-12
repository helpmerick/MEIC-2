"""ENT-10 / UI-24: the day supervisor — arming starts the wall-clock watch,
disarming stops it, and a crash is an alert, never a retry loop.

`_remaining_rows` and `_day_status_extras` are pure and tested directly.
`_supervise_once` is the supervisor's per-tick decision, factored out of
`live_app`'s startup loop specifically so it can be driven with dummy
comp/state/runtime objects instead of a real broker (see AMENDMENT-PROPOSAL-
arm-runs-the-day.md)."""
import asyncio
import types
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from meic.adapters.api.server import (
    _day_status_extras,
    _remaining_rows,
    _supervise_once,
    _supervisor_tick,
)
from meic.composition.live_runtime import ScheduledRow
from meic.domain.events import CondorFilled, EntrySkipped

NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
DAY = "2026-07-09"


def _row(minutes: int, number: int | None = None) -> ScheduledRow:
    return ScheduledRow(NOW + timedelta(minutes=minutes), number=number)


# --- _remaining_rows -----------------------------------------------------------

class TestRemainingRows:
    def test_filters_past_rows(self):
        rows = [_row(-10, number=1), _row(10, number=2)]
        out = _remaining_rows(rows, NOW, [], DAY)
        assert [r.number for r in out] == [2]

    def test_filters_rows_already_filled(self):
        rows = [_row(10, number=1), _row(20, number=2)]
        events = [CondorFilled(entry_id=f"{DAY}#1", net_credit=D("4.00"))]
        out = _remaining_rows(rows, NOW, events, DAY)
        assert [r.number for r in out] == [2]

    def test_filters_rows_already_skipped(self):
        rows = [_row(10, number=1), _row(20, number=2)]
        events = [EntrySkipped(date=DAY, entry_number=1, reason="max_entries")]
        out = _remaining_rows(rows, NOW, events, DAY)
        assert [r.number for r in out] == [2]

    def test_keeps_original_numbers_on_survivors(self):
        """ENT-10(4): filtering must never renumber — a survivor's number is
        exactly what it was stamped with, so its entry_id stays stable."""
        rows = [_row(10, number=5), _row(20, number=9)]
        out = _remaining_rows(rows, NOW, [], DAY)
        assert [r.number for r in out] == [5, 9]

    def test_falls_back_to_list_position_when_number_is_unset(self):
        rows = [_row(10), _row(20)]           # positions 1 and 2, both still future
        events = [CondorFilled(entry_id=f"{DAY}#2", net_credit=D("4.00"))]
        out = _remaining_rows(rows, NOW, events, DAY)
        assert len(out) == 1 and out[0].when == rows[0].when

    def test_a_matching_event_on_a_different_day_does_not_exclude(self):
        rows = [_row(10, number=1)]
        events = [CondorFilled(entry_id="2026-07-08#1", net_credit=D("4.00"))]
        out = _remaining_rows(rows, NOW, events, DAY)
        assert [r.number for r in out] == [1]


# --- _day_status_extras ----------------------------------------------------------

class TestDayStatusExtras:
    def test_empty_rows_yields_none_none_zero(self):
        assert _day_status_extras([], NOW) == {
            "next_entry_at": None, "seconds_to_next": None, "entries_remaining": 0}

    def test_computes_next_seconds_and_remaining_count(self):
        rows = [_row(30, number=1), _row(90, number=2)]
        out = _day_status_extras(rows, NOW)
        assert out["seconds_to_next"] == 1800
        assert out["entries_remaining"] == 2
        assert out["next_entry_at"] == rows[0].when.isoformat()

    def test_past_rows_are_excluded_from_remaining(self):
        rows = [_row(-10, number=1), _row(30, number=2)]
        out = _day_status_extras(rows, NOW)
        assert out["entries_remaining"] == 1
        assert out["next_entry_at"] == rows[1].when.isoformat()

    def test_an_already_attempted_entry_is_not_shown_as_next(self):
        """Review finding 3: /day/status feeds _day_status_extras the SAME
        _remaining_rows output the supervisor uses, so an entry that already
        fired today (e.g. an early ENT-09 manual fire, its time still in the
        future) no longer shows as next_entry / remaining."""
        rows = [_row(30, number=1), _row(90, number=2)]
        events = [CondorFilled(entry_id=f"{DAY}#1", net_credit=D("4.00"))]
        out = _day_status_extras(_remaining_rows(rows, NOW, events, DAY), NOW)
        assert out["entries_remaining"] == 1
        assert out["next_entry_at"] == rows[1].when.isoformat()   # #2, not the filled #1


# --- _supervise_once -------------------------------------------------------------

class _Alerts:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def alert(self, level, message, **context) -> None:
        self.calls.append((level, message, context))


class _FakeRuntime:
    """Records every run_day call; the coroutine it returns never completes on
    its own within a test — cancellation or a crash is driven explicitly."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def run_day(self, day, rows):
        self.calls.append((day, rows))
        await asyncio.sleep(3600)
        return 0   # pragma: no cover - never reached in these tests


def _comp(armed: bool):
    return types.SimpleNamespace(state=types.SimpleNamespace(armed=armed), events=[])


def test_supervise_once_arms_and_starts_a_task_for_the_remaining_rows():
    async def scenario():
        comp = _comp(armed=True)
        alerts = _Alerts()
        app_state = types.SimpleNamespace(day_task=None, day_task_failed=False)
        rows = [_row(10, number=1), _row(20, number=2)]
        runtime = _FakeRuntime()

        await _supervise_once(app_state, comp, alerts, lambda: rows, runtime, lambda: NOW)
        await asyncio.sleep(0)   # let the created task start executing

        assert app_state.day_task is not None and not app_state.day_task.done()
        assert len(runtime.calls) == 1
        day, passed_rows = runtime.calls[0]
        assert day == NOW.date().isoformat()
        assert [r.number for r in passed_rows] == [1, 2]

        app_state.day_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await app_state.day_task

    asyncio.run(scenario())


def test_supervise_once_disarm_cancels_the_running_task_and_clears_the_latch():
    async def scenario():
        comp = _comp(armed=True)
        alerts = _Alerts()
        app_state = types.SimpleNamespace(day_task=None, day_task_failed=False)
        rows = [_row(10, number=1)]
        runtime = _FakeRuntime()

        await _supervise_once(app_state, comp, alerts, lambda: rows, runtime, lambda: NOW)
        task = app_state.day_task
        assert task is not None and not task.done()

        app_state.day_task_failed = True     # simulate a stale latch from earlier
        comp.state.armed = False
        await _supervise_once(app_state, comp, alerts, lambda: rows, runtime, lambda: NOW)
        await asyncio.sleep(0)                # let the cancellation land

        assert app_state.day_task_failed is False   # ENT-10(6): a disarm clears the latch
        assert task.cancelled()                     # ENT-10(3): disarm stops the watch

    asyncio.run(scenario())


def test_supervise_once_latches_and_alerts_once_on_a_crash_without_restarting():
    async def scenario():
        comp = _comp(armed=True)
        alerts = _Alerts()
        rows = [_row(10, number=1)]
        runtime = _FakeRuntime()

        async def _boom():
            raise RuntimeError("boom")

        crashed = asyncio.create_task(_boom())
        await asyncio.sleep(0)   # let it run to completion (with an exception set)
        assert crashed.done()

        app_state = types.SimpleNamespace(day_task=crashed, day_task_failed=False)
        await _supervise_once(app_state, comp, alerts, lambda: rows, runtime, lambda: NOW)

        assert app_state.day_task_failed is True
        assert len(alerts.calls) == 1
        level, message, context = alerts.calls[0]
        assert level == "critical" and "ENT-10" in message and "boom" in context["error"]
        assert app_state.day_task is crashed        # not replaced with a new task
        assert runtime.calls == []                  # ENT-10(6): no restart on this pass

    asyncio.run(scenario())


def test_supervise_once_failed_latch_blocks_restart_until_disarm_then_rearm():
    async def scenario():
        comp = _comp(armed=True)
        alerts = _Alerts()
        rows = [_row(10, number=1)]
        runtime = _FakeRuntime()
        app_state = types.SimpleNamespace(day_task=None, day_task_failed=True)

        # still armed, still latched -> no restart
        await _supervise_once(app_state, comp, alerts, lambda: rows, runtime, lambda: NOW)
        assert app_state.day_task is None
        assert runtime.calls == []

        # disarm clears the latch
        comp.state.armed = False
        await _supervise_once(app_state, comp, alerts, lambda: rows, runtime, lambda: NOW)
        assert app_state.day_task_failed is False

        # re-arm -> the day can start again
        comp.state.armed = True
        await _supervise_once(app_state, comp, alerts, lambda: rows, runtime, lambda: NOW)
        await asyncio.sleep(0)
        assert app_state.day_task is not None
        assert len(runtime.calls) == 1

    asyncio.run(scenario())


class _CrashOnceRuntime:
    """run_day crashes on its FIRST call and hangs (like a real watching day) on
    every later one — the shape of a crash followed by an operator restart."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def run_day(self, day, rows):
        self.calls.append((day, rows))
        if len(self.calls) == 1:
            raise RuntimeError("boom")
        await asyncio.sleep(3600)
        return 0   # pragma: no cover - never reached in these tests


def test_supervise_once_disarm_then_rearm_after_a_crash_actually_restarts_the_day():
    """Regression (review finding 1). The disarm branch used to clear the crash
    latch but LEAVE app_state.day_task pointing at the crashed task; the re-arm
    tick then re-detected that same old exception, re-latched, re-alerted, and
    never started a new task — the disarm→arm cycle ENT-10(6) prescribes could
    never actually restart the day."""
    async def scenario():
        comp = _comp(armed=True)
        alerts = _Alerts()
        rows = [_row(10, number=1)]
        runtime = _CrashOnceRuntime()
        app_state = types.SimpleNamespace(day_task=None, day_task_failed=False)

        # tick 1: arm starts the day; run_day crashes (a REAL done-with-exception task)
        await _supervise_once(app_state, comp, alerts, lambda: rows, runtime, lambda: NOW)
        await asyncio.sleep(0)   # let the task run to its exception
        assert len(runtime.calls) == 1
        assert app_state.day_task.done() and app_state.day_task.exception() is not None

        # tick 2: crash detected -> latch + alert ONCE, no restart on this pass
        await _supervise_once(app_state, comp, alerts, lambda: rows, runtime, lambda: NOW)
        assert app_state.day_task_failed is True
        assert len(alerts.calls) == 1
        assert len(runtime.calls) == 1

        # tick 3: disarm -> latch cleared AND the stale task reference dropped
        comp.state.armed = False
        await _supervise_once(app_state, comp, alerts, lambda: rows, runtime, lambda: NOW)
        assert app_state.day_task_failed is False
        assert app_state.day_task is None

        # tick 4: re-arm -> run_day is called a SECOND time; the old exception is
        # not re-detected, so there is exactly ONE alert in total
        comp.state.armed = True
        await _supervise_once(app_state, comp, alerts, lambda: rows, runtime, lambda: NOW)
        await asyncio.sleep(0)
        assert len(runtime.calls) == 2
        assert len(alerts.calls) == 1
        assert app_state.day_task is not None and not app_state.day_task.done()

        app_state.day_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await app_state.day_task

    asyncio.run(scenario())


# --- _supervisor_tick: a broken tick is visible, and alerts once per error ------

def test_supervisor_tick_alerts_once_per_distinct_error_and_latches_it():
    """Review finding 3: `except Exception: pass` hid tick failures completely —
    a bug in the schedule read would silently prevent the day from ever starting.
    A failing tick must raise ONE critical alert per distinct error (not one every
    interval) and surface it as day_supervisor_error."""
    async def scenario():
        comp = _comp(armed=True)
        alerts = _Alerts()
        runtime = _FakeRuntime()
        app_state = types.SimpleNamespace(day_task=None, day_task_failed=False,
                                          day_supervisor_error=None)

        def broken_rows():
            raise RuntimeError("schedule read failed")

        await _supervisor_tick(app_state, comp, alerts, broken_rows, runtime, lambda: NOW)
        assert app_state.day_supervisor_error is not None
        assert "schedule read failed" in app_state.day_supervisor_error
        assert len(alerts.calls) == 1
        assert alerts.calls[0][0] == "critical" and "ENT-10" in alerts.calls[0][1]

        # same error on the next tick -> NO second alert (no 2-second spam)
        await _supervisor_tick(app_state, comp, alerts, broken_rows, runtime, lambda: NOW)
        assert len(alerts.calls) == 1

    asyncio.run(scenario())


def test_supervisor_tick_recovery_clears_the_error_and_a_new_error_alerts_again():
    async def scenario():
        comp = _comp(armed=True)    # armed with nothing composed: a healthy no-op tick
        alerts = _Alerts()
        runtime = _FakeRuntime()
        app_state = types.SimpleNamespace(day_task=None, day_task_failed=False,
                                          day_supervisor_error="RuntimeError('old')")

        # a clean tick clears the latch...
        await _supervisor_tick(app_state, comp, alerts, lambda: [], runtime, lambda: NOW)
        assert app_state.day_supervisor_error is None

        # ...so a DIFFERENT later failure alerts again
        def broken_rows():
            raise ValueError("new failure")
        await _supervisor_tick(app_state, comp, alerts, broken_rows, runtime, lambda: NOW)
        assert len(alerts.calls) == 1 and "new failure" in app_state.day_supervisor_error

    asyncio.run(scenario())


# --- wiring: live_app exposes the extended /day/status and the supervisor task --

@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Same isolation as test_live_app.py: never read the operator's real .env."""
    import os as _os
    from meic.adapters.api import server
    monkeypatch.setattr(server, "_read_env", lambda: dict(_os.environ))


def _jwt(iss: str) -> str:
    import base64
    import json

    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'EdDSA'})}.{seg({'iss': iss})}.sig"


def _cert_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TT_CERT_PROVIDER_SECRET", "s")
    monkeypatch.setenv("TT_CERT_REFRESH_TOKEN", _jwt("https://api.sandbox.tastyworks.com"))
    monkeypatch.setenv("TT_CERT_ACCOUNT", "5WZ00000")
    monkeypatch.setenv("MEIC_LIVE_IS_TEST", "true")
    monkeypatch.setenv("MEIC_DATA_DIR", str(tmp_path))


async def _async(v):
    return v


def test_day_status_endpoint_reports_armed_and_watch_state(monkeypatch, tmp_path):
    """Wiring capstone: /day/status carries the new ENT-10/UI-24 fields, and the
    supervisor task is actually running (not just wired but never started)."""
    from fastapi.testclient import TestClient
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    monkeypatch.setenv("MEIC_DAY_SUPERVISOR_INTERVAL_S", "0.05")

    app = live_app()
    comp = app.state.composition

    async def _noop_connect(account=None):
        return None
    comp.connect = _noop_connect
    comp.broker._inner.positions = lambda: _async([])
    comp.broker._inner.working_orders = lambda: _async([])
    comp.broker._inner.server_time = lambda: _async(datetime.now(timezone.utc))

    with TestClient(app) as client:
        assert isinstance(app.state.day_supervisor, asyncio.Task)
        assert app.state.day_task_failed is False

        r = client.get("/day/status")
        assert r.status_code == 200
        body = r.json()
        # nothing composed, never armed in this test -> the idle shape
        assert body["armed"] is False
        assert body["next_entry_at"] is None
        assert body["seconds_to_next"] is None
        assert body["entries_remaining"] == 0
        assert body["started"] is False
        assert body["running"] is False
        assert body["supervisor_error"] is None   # healthy ticks -> no latched error


# --- POST /day/start: the manual start shares the supervisor's guarantees -------

def _compose_schedule(tmp_path, *, rows=None):
    """Write a saved schedule into the SAME durable store live_app() will open
    (same helper as test_live_app.py)."""
    from meic.adapters.persistence.event_store import SqliteStateStore
    from meic.application.persistent_state import PersistentState
    from meic.application.schedule_service import ScheduleService

    state = PersistentState(SqliteStateStore(tmp_path / "state.db"))
    out = ScheduleService(state).save(rows or [{"time": "10:00", "contracts": 2},
                                               {"time": "11:15", "contracts": 1}],
                                      max_day_risk="20000")
    assert out["result"] == "saved", out


def _fake_broker(app):
    comp = app.state.composition

    async def _noop_connect(account=None):
        return None
    comp.connect = _noop_connect
    comp.broker._inner.positions = lambda: _async([])
    comp.broker._inner.working_orders = lambda: _async([])
    comp.broker._inner.server_time = lambda: _async(datetime.now(timezone.utc))
    return comp


def test_day_start_refuses_while_disarmed_and_persists_no_skips(monkeypatch, tmp_path):
    """Review finding 2 regression. A disarmed /day/start used to run the whole
    day anyway: every row skipped DISARMED, and those persisted EntrySkipped
    events made the remaining-rows filter treat the ENTIRE schedule as already
    attempted — silently disabling the day even after a later legitimate arm."""
    from fastapi.testclient import TestClient
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    monkeypatch.setenv("MEIC_DAY_SUPERVISOR_INTERVAL_S", "3600")
    _compose_schedule(tmp_path)

    app = live_app()
    comp = _fake_broker(app)

    with TestClient(app) as client:
        r = client.post("/day/start", headers={"x-api-token": "panel-secret"})
        assert r.status_code == 400
        assert r.json()["detail"] == "not_armed"

    # the refusal happened BEFORE run_day: nothing was skipped, nothing persisted
    assert not any(isinstance(e, EntrySkipped) for e in comp.events)
    assert app.state.day_task is None


def test_day_start_passes_only_the_remaining_rows_through_the_shared_filter(monkeypatch, tmp_path):
    """Review finding 2: /day/start must hand run_day exactly what the supervisor
    would — _remaining_rows' output, original numbers intact — never the raw
    schedule (which would re-attempt filled entries)."""
    from fastapi.testclient import TestClient
    from meic.adapters.api import server
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    monkeypatch.setenv("MEIC_DAY_SUPERVISOR_INTERVAL_S", "3600")
    _compose_schedule(tmp_path)

    app = live_app()
    comp = _fake_broker(app)

    captured: dict = {}

    async def fake_run_day(day, rows):
        captured["day"] = day
        captured["rows"] = rows
        return 0
    monkeypatch.setattr(app.state.runtime, "run_day", fake_run_day)

    filter_calls: list = []

    def fake_filter(rows, now, events, day):
        filter_calls.append((rows, now, events, day))
        return list(rows)[1:]     # "row 1 was already attempted"
    # the endpoint resolves _remaining_rows from module globals at call time,
    # so patching the module symbol intercepts the closure's call too
    monkeypatch.setattr(server, "_remaining_rows", fake_filter)

    with TestClient(app) as client:
        comp.state.armed = True   # arm AFTER startup; supervisor next ticks in 3600s
        r = client.post("/day/start", headers={"x-api-token": "panel-secret"})
        assert r.status_code == 200
        body = r.json()
        assert body["running"] is True
        assert body["entries"] == 1                      # the FILTERED count, not 2
        # cycle the loop until the created task has run the (instant) fake run_day
        for _ in range(50):
            if "rows" in captured:
                break
            client.get("/day/status")
        assert "rows" in captured, "day task never ran"

    assert len(filter_calls) == 1                        # the shared filter WAS used
    assert len(filter_calls[0][0]) == 2                  # ...over the full schedule
    assert [row.number for row in captured["rows"]] == [2]   # original number kept


def test_day_start_reports_no_remaining_entries_when_the_filter_leaves_nothing(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from meic.adapters.api import server
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    monkeypatch.setenv("MEIC_DAY_SUPERVISOR_INTERVAL_S", "3600")
    _compose_schedule(tmp_path)

    app = live_app()
    comp = _fake_broker(app)
    monkeypatch.setattr(server, "_remaining_rows", lambda rows, now, events, day: [])

    with TestClient(app) as client:
        comp.state.armed = True
        r = client.post("/day/start", headers={"x-api-token": "panel-secret"})
        assert r.status_code == 200
        assert r.json() == {"running": False, "reason": "no_remaining_entries"}
    assert app.state.day_task is None


# --- GET /calendar/adjacent-trading-day: DAY-01 session stepping for the Results
# page's Day arrows (weekends AND market holidays skipped, never into the future) --

def _calendar_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    monkeypatch.setenv("MEIC_DAY_SUPERVISOR_INTERVAL_S", "3600")

    app = live_app()
    _fake_broker(app)
    return TestClient(app)


def test_adjacent_trading_day_prev_skips_a_plain_weekend(monkeypatch, tmp_path):
    """Monday 2020-07-13 has no adjacent holiday: prev must land on the
    previous Friday, 2020-07-10. (A safely-past year so the endpoint's
    never-into-the-future cap on `next` can't interfere with this `prev` case.)"""
    with _calendar_client(monkeypatch, tmp_path) as client:
        r = client.get("/calendar/adjacent-trading-day", params={"from": "2020-07-13", "dir": "prev"})
        assert r.status_code == 200
        assert r.json() == {"date": "2020-07-10"}


def test_adjacent_trading_day_next_skips_a_plain_weekend(monkeypatch, tmp_path):
    """Friday 2020-07-10 has no adjacent holiday: next must land on the
    following Monday, 2020-07-13. Safely in the past so it lands well below
    the endpoint's never-into-the-future cap regardless of when this runs."""
    with _calendar_client(monkeypatch, tmp_path) as client:
        r = client.get("/calendar/adjacent-trading-day", params={"from": "2020-07-10", "dir": "next"})
        assert r.status_code == 200
        assert r.json() == {"date": "2020-07-13"}


def test_adjacent_trading_day_prev_skips_both_a_holiday_and_the_weekend(monkeypatch, tmp_path):
    """2020's Independence Day is OBSERVED on Friday 2020-07-03 (July 4th
    itself falls on a Saturday, per nyse_holidays). Stepping back from Monday
    2020-07-06 must skip Sun 07-05, Sat 07-04, AND the observed holiday Fri
    07-03, landing on Thursday 2020-07-02 -- verified against nyse_holidays(2020)
    + is_trading_day, not eyeballed off a calendar."""
    from meic.application.market_calendar import is_trading_day
    from meic.application.nyse_holidays import nyse_holidays

    holidays = nyse_holidays(2019) | nyse_holidays(2020) | nyse_holidays(2021)
    assert date.fromisoformat("2020-07-03") in holidays          # observed Independence Day
    assert is_trading_day(date.fromisoformat("2020-07-02"), holidays=holidays)
    assert not is_trading_day(date.fromisoformat("2020-07-03"), holidays=holidays)

    with _calendar_client(monkeypatch, tmp_path) as client:
        r = client.get("/calendar/adjacent-trading-day", params={"from": "2020-07-06", "dir": "prev"})
        assert r.status_code == 200
        assert r.json() == {"date": "2020-07-02"}


def test_adjacent_trading_day_next_returns_null_well_past_today(monkeypatch, tmp_path):
    """`next` must never navigate the Results Day picker into the future."""
    with _calendar_client(monkeypatch, tmp_path) as client:
        r = client.get("/calendar/adjacent-trading-day", params={"from": "2099-12-31", "dir": "next"})
        assert r.status_code == 200
        assert r.json() == {"date": None}


def test_adjacent_trading_day_next_returns_a_session_well_before_today(monkeypatch, tmp_path):
    with _calendar_client(monkeypatch, tmp_path) as client:
        r = client.get("/calendar/adjacent-trading-day", params={"from": "2020-01-01", "dir": "next"})
        assert r.status_code == 200
        body = r.json()
        assert body["date"] is not None
        assert body["date"] > "2020-01-01"


def test_adjacent_trading_day_rejects_a_bad_from_date(monkeypatch, tmp_path):
    with _calendar_client(monkeypatch, tmp_path) as client:
        r = client.get("/calendar/adjacent-trading-day", params={"from": "nonsense", "dir": "prev"})
        assert r.status_code == 422


def test_adjacent_trading_day_rejects_a_bad_dir(monkeypatch, tmp_path):
    with _calendar_client(monkeypatch, tmp_path) as client:
        r = client.get("/calendar/adjacent-trading-day", params={"from": "2026-07-13", "dir": "sideways"})
        assert r.status_code == 422
