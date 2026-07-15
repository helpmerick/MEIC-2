"""NFR-02 / RSK-06 health-loop guard (v1.74 fix batch, approved as proposed).

Before this fix, `server.py::_start_health_loop`'s inner `_loop()` had no
try/except at all around its per-tick body: an unhandled exception from
`_probe_once()` would kill `health_task` outright, silently, with nothing
watching it — NFR-02's clock/session-liveness reading would go stale forever
with no alert naming why. Fixed by mirroring `_supervisor_tick`'s exact
pattern (try/except + alert-once-per-distinct-error latch) plus a
done-callback on `health_task` that alerts CRITICAL if the task itself ever
dies (the `attempt_crash` alert-only pattern).

Pin: a probe that raises once => one alert, the loop continues, the next
tick runs; task death => CRITICAL alert.
"""
import asyncio
import types

import pytest

from meic.adapters.api.server import _health_task_done_callback, _health_tick


class _Alerts:
    def __init__(self):
        self.calls: list[tuple] = []

    def alert(self, level, message, **context):
        self.calls.append((level, message, context))


def _app_state(**kw):
    kw.setdefault("health_loop_error", None)
    return types.SimpleNamespace(**kw)


# --- _health_tick: a broken tick is visible, alerts once per distinct error --

def test_health_tick_alerts_once_per_distinct_error_and_latches_it():
    """A failing tick must raise ONE critical alert per distinct error (not
    one every interval) and surface it on health_loop_error."""
    async def scenario():
        app_state = _app_state()
        alerts = _Alerts()

        async def broken_probe():
            raise RuntimeError("broker session probe failed")

        await _health_tick(app_state, alerts, broken_probe)
        assert app_state.health_loop_error is not None
        assert "broker session probe failed" in app_state.health_loop_error
        assert len(alerts.calls) == 1
        assert alerts.calls[0][0] == "critical" and "NFR-02" in alerts.calls[0][1]

        # same error on the next tick -> NO second alert
        await _health_tick(app_state, alerts, broken_probe)
        assert len(alerts.calls) == 1

    asyncio.run(scenario())


def test_health_tick_survives_the_exception_and_the_next_tick_still_runs():
    """Pin: a probe that raises once must not stop the loop -- the very next
    tick, with a healthy probe, must still run to completion."""
    async def scenario():
        app_state = _app_state()
        alerts = _Alerts()
        calls = {"n": 0}

        async def probe_raises_once():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")

        await _health_tick(app_state, alerts, probe_raises_once)   # tick 1: raises, caught
        assert len(alerts.calls) == 1
        await _health_tick(app_state, alerts, probe_raises_once)   # tick 2: must still run
        assert calls["n"] == 2
        assert app_state.health_loop_error is None   # the clean tick clears the latch

    asyncio.run(scenario())


def test_health_tick_recovery_clears_the_error_and_a_new_error_alerts_again():
    async def scenario():
        app_state = _app_state(health_loop_error="RuntimeError('old')")
        alerts = _Alerts()

        async def healthy_probe():
            return None

        await _health_tick(app_state, alerts, healthy_probe)
        assert app_state.health_loop_error is None

        async def new_failure():
            raise ValueError("new failure")

        await _health_tick(app_state, alerts, new_failure)
        assert len(alerts.calls) == 1 and "new failure" in app_state.health_loop_error

    asyncio.run(scenario())


# --- _health_task_done_callback: task death is a CRITICAL alert -------------

def test_health_task_done_callback_alerts_critical_when_task_dies():
    async def scenario():
        async def _boom():
            raise RuntimeError("event loop died")

        crashed = asyncio.create_task(_boom())
        await asyncio.sleep(0)   # let it run to completion (exception set)
        assert crashed.done()

        alerts = _Alerts()
        _health_task_done_callback(alerts)(crashed)

        assert len(alerts.calls) == 1
        level, message, _ctx = alerts.calls[0]
        assert level == "critical" and "NFR-02" in message and "health_task died" in message

    asyncio.run(scenario())


def test_health_task_done_callback_ignores_deliberate_cancellation():
    """A cancelled task (normal shutdown, see `_stop_health_loop`) is not a
    crash and must never alert."""
    async def scenario():
        async def _forever():
            await asyncio.sleep(3600)

        task = asyncio.create_task(_forever())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

        alerts = _Alerts()
        _health_task_done_callback(alerts)(task)
        assert alerts.calls == []

    asyncio.run(scenario())


def test_health_task_done_callback_is_a_noop_on_clean_completion():
    """A task that completes with no exception (should not happen for the
    real infinite `_loop`, but the callback must handle it) never alerts."""
    async def scenario():
        async def _clean():
            return None

        task = asyncio.create_task(_clean())
        await asyncio.sleep(0)
        assert task.done()

        alerts = _Alerts()
        _health_task_done_callback(alerts)(task)
        assert alerts.calls == []

    asyncio.run(scenario())


# --- fail-first: reproduce the PRE-FIX vulnerability directly ---------------

async def _unguarded_loop_body(probe_once) -> None:
    """The exact shape `_start_health_loop`'s `_loop()` had before this fix:
    no try/except at all around the per-tick body."""
    await probe_once()


def test_fail_first_the_old_unguarded_tick_shape_lets_the_task_die_silently():
    """FAIL-FIRST evidence: replicate the PRE-FIX `_loop()` body verbatim (no
    guard) and show a single probe exception kills the task outright, with
    no alert raised at all -- exactly the silent death this fix closes.
    Contrast with `_health_tick`, which survives the identical probe."""
    async def scenario():
        async def broken_probe():
            raise RuntimeError("boom")

        task = asyncio.create_task(_unguarded_loop_body(broken_probe))
        await asyncio.sleep(0)
        assert task.done() and task.exception() is not None, (
            "the unguarded shape must crash on a single tick exception")

        # the SAME probe, through the fixed guard, survives and alerts instead.
        app_state = _app_state()
        alerts = _Alerts()
        await _health_tick(app_state, alerts, broken_probe)  # does not raise
        assert len(alerts.calls) == 1

    asyncio.run(scenario())
