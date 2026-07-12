"""DAY-01 / UI-24 / ENT-10 (operator ruling 2026-07-11): the day supervisor and
/day/status consult the exchange calendar.

The reported bug: on Saturday 2026-07-11 the panel promised "Next entry 11:56
ET — in 7:03:05". Two halves fixed here:

  * `_supervise_once` starts NO day task on a weekend/market holiday — before,
    every closed day got a task whose entries were then each refused at fire
    time by the ENT-03 market-open gate (still in place as the safety net),
    writing EntrySkipped noise into the event log.

  * /day/status rolls the countdown to the NEXT trading day's first entry on a
    closed day. On a trading day nothing changes: an exhausted schedule still
    reads "no more entries today" (TC-UI-06 locks that wording).
"""
import asyncio
import types
from datetime import datetime, timedelta, timezone

from meic.adapters.api.server import _supervise_once
from meic.composition.live_gates import ET
from meic.composition.live_runtime import ScheduledRow

SATURDAY = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)      # 08:00 ET Sat
GOOD_FRIDAY = datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc)    # 08:00 ET Fri (holiday)
THURSDAY = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)       # 08:00 ET Thu (open)


class _Alerts:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def alert(self, level, message, **context) -> None:
        self.calls.append((level, message, context))


class _FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def run_day(self, day, rows):
        self.calls.append((day, rows))
        await asyncio.sleep(3600)
        return 0   # pragma: no cover — cancelled before completion in tests


def _comp(armed: bool):
    return types.SimpleNamespace(state=types.SimpleNamespace(armed=armed), events=[])


def _drive(now):
    """One armed supervisor tick at `now` with a future row; returns what ran."""
    async def scenario():
        comp = _comp(armed=True)
        app_state = types.SimpleNamespace(day_task=None, day_task_failed=False)
        rows = [ScheduledRow(now + timedelta(minutes=10), number=1)]
        runtime = _FakeRuntime()
        await _supervise_once(app_state, comp, _Alerts(), lambda: rows, runtime, lambda: now)
        await asyncio.sleep(0)
        started = app_state.day_task is not None
        if started:
            app_state.day_task.cancel()
            try:
                await app_state.day_task
            except asyncio.CancelledError:
                pass
        return started, runtime.calls
    return asyncio.run(scenario())


class TestSupervisorCalendarGate:
    def test_day01_no_day_task_on_a_saturday(self):
        started, calls = _drive(SATURDAY)
        assert started is False and calls == []

    def test_day01_no_day_task_on_a_market_holiday(self):
        started, calls = _drive(GOOD_FRIDAY)
        assert started is False and calls == []

    def test_day01_a_trading_day_still_starts_the_watch(self):
        started, calls = _drive(THURSDAY)
        assert started is True and len(calls) == 1


# --- /day/status rolls to the next trading day on closed days -------------------
#
# Same live_app harness as tests/application/test_day_supervisor.py; the clock
# is frozen by patching the `datetime` symbol server.py resolves at call time.

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Never read the operator's real .env (same as test_live_app.py)."""
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
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    monkeypatch.setenv("MEIC_DAY_SUPERVISOR_INTERVAL_S", "3600")


def _compose_schedule(tmp_path):
    from meic.adapters.persistence.event_store import SqliteStateStore
    from meic.application.persistent_state import PersistentState
    from meic.application.schedule_service import ScheduleService

    state = PersistentState(SqliteStateStore(tmp_path / "state.db"))
    out = ScheduleService(state).save([{"time": "10:00", "contracts": 2},
                                       {"time": "11:15", "contracts": 1}],
                                      max_day_risk="20000")
    assert out["result"] == "saved", out


async def _async(v):
    return v


def _fake_broker(app):
    comp = app.state.composition

    async def _noop_connect(account=None):
        return None
    comp.connect = _noop_connect
    comp.broker._inner.positions = lambda: _async([])
    comp.broker._inner.working_orders = lambda: _async([])
    comp.broker._inner.server_time = lambda: _async(datetime.now(timezone.utc))
    return comp


def _freeze(monkeypatch, frozen: datetime):
    """Freeze every datetime.now() server.py performs (it resolves the module
    global at call time, so patching the symbol is enough)."""
    from meic.adapters.api import server

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz is not None else frozen.replace(tzinfo=None)

    monkeypatch.setattr(server, "datetime", _FrozenDT)


def _status_at(monkeypatch, tmp_path, frozen: datetime) -> dict:
    from fastapi.testclient import TestClient
    from meic.adapters.api.server import live_app

    _cert_env(monkeypatch, tmp_path)
    _compose_schedule(tmp_path)
    _freeze(monkeypatch, frozen)

    app = live_app()
    _fake_broker(app)
    with TestClient(app) as client:
        r = client.get("/day/status")
        assert r.status_code == 200
        return r.json()


class TestDayStatusCalendarRollover:
    def test_day01_ui24_saturday_rolls_to_mondays_first_entry(self, monkeypatch, tmp_path):
        # The screenshot bug, exactly: Saturday 2026-07-11 09:53 ET.
        body = _status_at(monkeypatch, tmp_path,
                          datetime(2026, 7, 11, 9, 53, tzinfo=ET))
        assert body["next_entry_at"] == "2026-07-13T10:00:00-04:00"
        assert body["seconds_to_next"] == 2 * 86400 + 7 * 60   # Sat 09:53 -> Mon 10:00
        assert body["entries_remaining"] == 2

    def test_day01_ui24_an_observed_holiday_rolls_past_the_long_weekend(self, monkeypatch, tmp_path):
        # Friday 2026-07-03 is the observed July 4th -> next session Monday the 6th.
        body = _status_at(monkeypatch, tmp_path,
                          datetime(2026, 7, 3, 9, 53, tzinfo=ET))
        assert body["next_entry_at"] == "2026-07-06T10:00:00-04:00"
        assert body["entries_remaining"] == 2

    def test_ui24_a_trading_day_exhausted_schedule_still_reads_no_more_entries(self, monkeypatch, tmp_path):
        # TC-UI-06's locked wording depends on this shape staying put on a
        # REAL trading day whose entries have all passed.
        body = _status_at(monkeypatch, tmp_path,
                          datetime(2026, 7, 9, 17, 0, tzinfo=ET))   # Thu after close
        assert body["next_entry_at"] is None
        assert body["seconds_to_next"] is None
        assert body["entries_remaining"] == 0
