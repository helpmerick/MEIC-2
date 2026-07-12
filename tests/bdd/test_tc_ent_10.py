"""TC-ENT-10 — ENT-10 arm-runs-the-day (v1.53, operator-ratified).

Entering ARMED starts a runtime task that watches the wall clock and fires each
composed entry at its time; the same loop restores itself on boot, stops
atomically on disarm, and alerts (never auto-restarts) on a crash. The v1.53
addition is the operator's ruling on identity: every schedule row gets a
DURABLE entry id at Save, and the day task / ORD-04 idempotency / exposure book
/ attempted-today tracking all key on that id, never on list position — so a
mid-day delete/re-save while ARMED can never renumber a survivor, drop it, or
fire it twice.

Heavy async driving lives in `@given`/`@when` steps (the house pattern — see
tests/bdd/test_tc_ord_01.py): each does its own `asyncio.run(...)` and stashes
plain (non-asyncio) results on `world`; `@then` steps only do synchronous
assertions, since an asyncio Task created in one `asyncio.run` call cannot be
awaited from a different one.
"""
from __future__ import annotations

import asyncio
import types
from datetime import date, datetime, timedelta, timezone

import pytest
from pytest_bdd import given, scenarios, then, when
from decimal import Decimal as D

from meic.adapters.api.server import _remaining_rows, _supervise_once
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.persistent_state import PersistentState
from meic.application.schedule_service import ScheduleService
from meic.composition.live_runtime import LiveRuntime, ScheduledRow
from meic.composition.live_wiring import schedule_rows
from meic.domain.events import CondorFilled, EntrySkipped
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_clock import ET, FakeClock
from tests.harness.live_broker import LiveShapedBroker

scenarios("../features/TC-ENT-10.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
TODAY = date(2026, 7, 9)
DAY = TODAY.isoformat()
NOW = datetime(2026, 7, 9, 9, 45, tzinfo=ET)

GATES_PASS = GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                          flatten_in_progress=False, market_open=True, market_halted=False,
                          data_fresh=True, session_valid=True, buying_power_ok=True)


class _Alerts:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def alert(self, level, message, **context) -> None:
        self.calls.append((level, message, context))


def _comp(clock, broker):
    """Minimal live-shaped composition stand-in — the same shape LiveRuntime
    and ExecuteEntryAttempt actually consume (see test_live_runtime_numbers.py
    and test_live_fill_path.py)."""
    comp = types.SimpleNamespace()
    comp.clock = clock
    comp.broker = broker
    comp.events: list = []
    comp.state = PersistentState(InMemoryStateStore())
    comp.state.armed = False
    comp.state.confirm_live = True
    comp.state.stop_trading = False
    comp.execute = ExecuteEntryAttempt(broker, clock, comp.events, SPX)
    comp.protected: list[str] = []

    async def on_filled(entry_id, condor, stop=None, fill_credit=None):
        comp.protected.append(entry_id)
    comp._on_filled = on_filled
    return comp


async def _selector(when, n, config=None):
    """Fires whatever row it's asked for, keyed by the row's OWN durable `n` —
    exactly what run_day passes as `condor.entry_number`."""
    return Condor(entry_number=n, put_short=D("5990"), call_short=D("6060"),
                 put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                 mid_credit=D("4.00"), min_total_credit=D("2.00")), None


async def _gates():
    return GATES_PASS


@pytest.fixture
def world():
    return {}


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Same isolation as test_day_supervisor.py: never read the operator's
    real .env when a scenario boots a real live_app()."""
    import os as _os
    from meic.adapters.api import server
    monkeypatch.setattr(server, "_read_env", lambda: dict(_os.environ))


async def _async(v):
    return v


# --- Scenario 1: arm starts the watcher, the entry fires through the full gate chain

@given("a composed schedule with one future entry")
def _(world):
    clock = FakeClock(NOW)
    broker = LiveShapedBroker(clock, fill_delay=3.0)
    comp = _comp(clock, broker)
    when = NOW + timedelta(seconds=30)
    world.update(
        clock=clock, broker=broker, comp=comp,
        rows=[ScheduledRow(when, number=1)],
        runtime=LiveRuntime(comp, selector=_selector, market_gates=_gates),
        app_state=types.SimpleNamespace(day_task=None, day_task_failed=False),
        alerts=_Alerts(),
    )


@when("the operator arms successfully")
def _(world):
    async def scenario():
        comp, clock = world["comp"], world["clock"]
        comp.state.armed = True   # pre-flight already passed -- this IS the Arm
        await _supervise_once(world["app_state"], comp, world["alerts"],
                              lambda: world["rows"], world["runtime"], clock.now)
        await asyncio.sleep(0)   # let the created task reach its first await

        task = world["app_state"].day_task
        world["was_watching"] = task is not None and not task.done()

        # drive the clock until the entry fires (or the task otherwise finishes)
        for _ in range(6000):
            if task.done():
                break
            for _ in range(6):
                await asyncio.sleep(0)
            clock.advance(seconds=1)

        world["task_done_cleanly"] = task.done() and task.exception() is None
    asyncio.run(scenario())


@then("the day task is watching and the entry fires at its time through the full gate chain")
def _(world):
    assert world["was_watching"] is True
    assert world["task_done_cleanly"] is True
    comp = world["comp"]
    assert sum(isinstance(e, CondorFilled) for e in comp.events) == 1
    assert comp.protected == [f"{DAY}#1"]


# --- Scenario 2: boot restore resumes the watcher, no operator action ------------

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


@given("persisted state is ARMED with entries remaining")
def _(world, monkeypatch, tmp_path):
    from meic.adapters.api import server
    from meic.adapters.persistence.event_store import SqliteStateStore

    _cert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")
    monkeypatch.setenv("MEIC_DAY_SUPERVISOR_INTERVAL_S", "0.05")

    # Freeze server.py's `datetime.now(...)` (both "today" for schedule_rows and
    # "now" for the remaining-rows filter) so "10:00/11:15 ET" is deterministically
    # in the future regardless of the real wall-clock time the suite runs at.
    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW
    monkeypatch.setattr(server, "datetime", _FrozenDatetime)

    # persist a legal, ARMED schedule BEFORE the bot ever boots -- REC-07: a
    # restart must never turn a watching bot inert.
    state = PersistentState(SqliteStateStore(tmp_path / "state.db"))
    out = ScheduleService(state).save(
        [{"time": "10:00", "contracts": 1}, {"time": "11:15", "contracts": 1}],
        max_day_risk="20000")
    assert out["result"] == "saved", out
    state.armed = True

    world["tmp_path"] = tmp_path


@when("the bot boots")
def _(world):
    from fastapi.testclient import TestClient
    from meic.adapters.api.server import live_app

    app = live_app()
    comp = app.state.composition

    async def _noop_connect(account=None):
        return None
    comp.connect = _noop_connect
    comp.broker._inner.positions = lambda: _async([])
    comp.broker._inner.working_orders = lambda: _async([])
    comp.broker._inner.server_time = lambda: _async(datetime.now(timezone.utc))

    started = False
    with TestClient(app) as client:
        for _ in range(50):
            if app.state.day_task is not None:
                started = True
                break
            client.get("/day/status")
    world["started_without_operator_action"] = started


@then("the day task starts automatically without operator action")
def _(world):
    assert world["started_without_operator_action"] is True


# --- Scenario 3: disarm stops future entries atomically --------------------------

@given("an entry attempt is in flight when the operator disarms")
def _(world):
    async def scenario():
        clock = FakeClock(NOW)
        broker = LiveShapedBroker(clock, fill_delay=3.0)
        comp = _comp(clock, broker)
        comp.state.armed = True

        when1 = NOW                            # due immediately -- no wait_until block
        when2 = NOW + timedelta(seconds=600)   # a later entry that must NEVER fire
        rows = [ScheduledRow(when1, number=1), ScheduledRow(when2, number=2)]
        runtime = LiveRuntime(comp, selector=_selector, market_gates=_gates)

        day_task = asyncio.ensure_future(runtime.run_day(DAY, rows))

        # let the FIRST attempt reach the broker -- genuinely mid-ladder, before
        # the clock has advanced far enough for it to fill
        for _ in range(500):
            await asyncio.sleep(0)
            if broker.submits:
                break
        assert broker.submits, "the attempt never reached the broker"
        assert not any(isinstance(e, CondorFilled) for e in comp.events)

        comp.state.armed = False           # the operator disarms
        day_task.cancel()                  # ENT-10(3): the disarm/stop path
        with pytest.raises(asyncio.CancelledError):
            await day_task                 # the day task dies INSTANTLY

        # drive the clock well past BOTH entries' times so the shielded first
        # attempt runs to its natural end, and the second entry's time also passes
        for _ in range(700):
            for _ in range(6):
                await asyncio.sleep(0)
            clock.advance(seconds=1)

        world["comp"] = comp
        world["working_after"] = await broker.working_orders()
    asyncio.run(scenario())


@then("the attempt completes or cancels cleanly and is never abandoned mid-flight")
def _(world):
    comp = world["comp"]
    assert sum(isinstance(e, CondorFilled) for e in comp.events) == 1
    assert comp.protected == [f"{DAY}#1"]
    assert world["working_after"] == []   # no orphaned working entry order


@then("no further entries fire")
def _(world):
    comp = world["comp"]
    # entry #2's time passed too, but the day task died on cancel and never
    # reached it -- no fill, and no EntrySkipped either (it was never attempted).
    assert sum(isinstance(e, CondorFilled) for e in comp.events) == 1
    assert not any(isinstance(e, EntrySkipped) for e in comp.events)


# --- Scenario 4: durable ids survive a mid-day delete + re-save while ARMED ------

@given("rows A(fired), B(pending 11:15), C(pending 12:35) with durable ids")
def _(world):
    state = PersistentState(InMemoryStateStore())
    out = ScheduleService(state).save(
        [{"time": "10:00", "contracts": 1}, {"time": "11:15", "contracts": 1},
         {"time": "12:35", "contracts": 1}], max_day_risk="20000")
    assert out["result"] == "saved", out
    assert [r["id"] for r in state.entry_schedule] == [1, 2, 3]   # A, B, C

    clock = FakeClock(datetime(2026, 7, 9, 10, 30, tzinfo=ET))   # after A's time, before B's
    broker = LiveShapedBroker(clock, fill_delay=1.0)
    comp = _comp(clock, broker)
    comp.state.armed = True
    comp.events.append(CondorFilled(entry_id=f"{DAY}#1", net_credit=D("4.00")))  # A already fired

    world.update(state=state, comp=comp, clock=clock, broker=broker)


@when("the operator deletes fired row A while ARMED")
def _(world):
    state = world["state"]
    remaining = state.entry_schedule[1:]   # A deleted; B, C submitted VERBATIM (with their ids)
    # ENT-10(4): the caller derives used_ids from today's events -- id 1 already
    # fired in the schedule lane (<101) -- so it can never be reissued even
    # though A is no longer in the persisted schedule at all.
    out = ScheduleService(state).save(remaining, max_day_risk="20000", used_ids=1)
    assert out["result"] == "saved", out
    world["save_out"] = out


@then("rows B and C keep their ids, B fires at 11:15, and nothing is skipped or double-fired")
def _(world):
    ids = [r["id"] for r in world["save_out"]["rows"]]
    assert ids == [2, 3]   # B, C -- unchanged, never renumbered to 1, 2

    state, comp, clock = world["state"], world["comp"], world["clock"]
    rows = schedule_rows(state, today=TODAY, tz=ET)
    remaining = _remaining_rows(rows, clock.now(), comp.events, DAY)
    assert [r.number for r in remaining] == [2, 3]   # exactly B, C -- A (filled) excluded

    async def scenario():
        runtime = LiveRuntime(comp, selector=_selector, market_gates=_gates)
        task = asyncio.ensure_future(runtime.run_day(DAY, remaining))
        for _ in range(2000):
            if task.done():
                break
            for _ in range(6):
                await asyncio.sleep(0)
            clock.advance(seconds=5)
        await task
    asyncio.run(scenario())

    filled_ids = sorted(e.entry_id for e in comp.events if isinstance(e, CondorFilled))
    assert filled_ids == [f"{DAY}#1", f"{DAY}#2", f"{DAY}#3"]   # A (pre-existing), B, C -- no dupes
    assert not any(isinstance(e, EntrySkipped) for e in comp.events)


# --- Scenario 5: a crashed day task alerts and stays down ------------------------

class _CrashRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def run_day(self, day, rows):
        self.calls.append((day, rows))
        raise RuntimeError("boom")


@given("the day task dies with an error while ARMED")
def _(world):
    async def scenario():
        comp = types.SimpleNamespace(state=types.SimpleNamespace(armed=True), events=[])
        alerts = _Alerts()
        app_state = types.SimpleNamespace(day_task=None, day_task_failed=False)
        rows = [ScheduledRow(NOW + timedelta(minutes=10), number=1)]
        runtime = _CrashRuntime()

        # tick 1: arm starts the day; run_day crashes immediately
        await _supervise_once(app_state, comp, alerts, lambda: rows, runtime, lambda: NOW)
        await asyncio.sleep(0)   # let it run to its exception
        # tick 2: the crash is DETECTED -> latch + alert, no restart on this pass
        await _supervise_once(app_state, comp, alerts, lambda: rows, runtime, lambda: NOW)

        world.update(app_state=app_state, alerts=alerts, runtime=runtime, comp=comp, rows=rows)
    asyncio.run(scenario())


@then("a critical alert is raised and the task is NOT auto-restarted until Disarm then Arm")
def _(world):
    async def scenario():
        app_state, alerts, runtime, comp, rows = (
            world["app_state"], world["alerts"], world["runtime"], world["comp"], world["rows"])

        assert app_state.day_task_failed is True
        assert len(alerts.calls) == 1
        level, message, ctx = alerts.calls[0]
        assert level == "critical" and "ENT-10" in message and "boom" in ctx["error"]

        # still armed, still latched -> ticking again does NOT restart it
        await _supervise_once(app_state, comp, alerts, lambda: rows, runtime, lambda: NOW)
        assert len(runtime.calls) == 1
        assert len(alerts.calls) == 1

        # Disarm -> the crash latch clears (ENT-10(6))
        comp.state.armed = False
        await _supervise_once(app_state, comp, alerts, lambda: rows, runtime, lambda: NOW)
        assert app_state.day_task_failed is False

        # Arm -> the day actually restarts; no new alert from the restart itself
        comp.state.armed = True
        await _supervise_once(app_state, comp, alerts, lambda: rows, runtime, lambda: NOW)
        await asyncio.sleep(0)
        assert len(runtime.calls) == 2
        assert len(alerts.calls) == 1
    asyncio.run(scenario())
