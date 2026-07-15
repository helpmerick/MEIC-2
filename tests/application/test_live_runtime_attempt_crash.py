"""ENT-10/RSK-06: run_day's fire-and-forget attempt task must never again die
silently. 2026-07-14 production incident: entry 2026-07-14#2 journaled
EntryWindowOpened + CondorProposed and then NOTHING -- no fill, no skip. The
attempt task had crashed (the sqlite shared-connection race hotfixed
alongside this) and its exception was never retrieved.

These pin the done-callback (`_alert_and_journal_crashed_attempt`,
composition/live_runtime.py):
  * a crash BEFORE any fill -> critical alert + EntrySkipped(reason=
    "attempt_crashed:<ExcType>"), exactly once.
  * a crash AFTER CondorFilled already journaled (e.g. mid the STP-01
    `_on_filled` hand-off) -> alert, but NO EntrySkipped -- an entry that
    actually filled must never be mislabeled as skipped.
  * a healthy fill -> neither an alert nor a skip.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal as D

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor
from meic.application.persistent_state import PersistentState
from meic.composition.live_runtime import LiveRuntime
from meic.domain.events import CondorFilled, EntrySkipped
from tests.harness.fake_clock import ET

OPEN = datetime(2026, 7, 14, 9, 32, tzinfo=ET)
DAY = "2026-07-14"

GATES_PASS = GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                          flatten_in_progress=False, market_open=True, market_halted=False,
                          data_fresh=True, session_valid=True, buying_power_ok=True)


class FastClock:
    """wait_until jumps to the deadline instead of blocking (see test_live_runtime.py)."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    async def wait_until(self, when: datetime) -> None:
        if when > self._now:
            self._now = when


@dataclass
class _Outcome:
    status: str
    fill_credit: D | None = None
    reason: str | None = None


class _Alerts:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def alert(self, level, message, **context) -> None:
        self.calls.append((level, message, context))


def _condor(n=1) -> Condor:
    return Condor(entry_number=n, put_short=D("5990"), call_short=D("6060"),
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00"))


class _Comp:
    """Minimal stand-in for LiveComposition. `execute` is swapped per-test for
    a scripted double so the attempt can be made to crash at a chosen point,
    without needing a real broker/ExecuteEntryAttempt."""

    def __init__(self, clock, *, on_filled=None) -> None:
        self.clock = clock
        self.events: list = []
        self.state = PersistentState(InMemoryStateStore())
        self.state.armed = True
        self.state.confirm_live = True
        self.state.stop_trading = False
        self.alerts = _Alerts()
        self.execute = None  # set per-test
        self.protected: list[str] = []
        self._on_filled_override = on_filled

    async def _on_filled(self, entry_id, condor, stop=None, fill_credit=None):
        if self._on_filled_override is not None:
            await self._on_filled_override(entry_id, condor, stop, fill_credit)
            return
        self.protected.append(entry_id)


class _CrashBeforeFill:
    """Stand-in ExecuteEntryAttempt: the ladder itself blows up -- no
    CondorFilled is ever journaled. `**kwargs` keeps the double tolerant of
    additive attempt() parameters (e.g. cal-slice-1's `filters=`), same as
    every manual-path double already is."""

    async def attempt(self, *, day, scheduled, condor, gates, risk, stop, **kwargs):
        raise RuntimeError("boom-mid-ladder")


class _FillsThenComp_OnFilledCrashes:
    """Stand-in ExecuteEntryAttempt that fills exactly like the real one does
    -- journals CondorFilled BEFORE returning -- and returns FILLED; the
    crash then happens in the STP-01 hand-off (comp._on_filled), which this
    test's `_Comp` is wired to raise."""

    def __init__(self, comp) -> None:
        self._comp = comp

    async def attempt(self, *, day, scheduled, condor, gates, risk, stop, **kwargs):
        entry_id = f"{day}#{condor.entry_number}"
        self._comp.events.append(CondorFilled(entry_id=entry_id, net_credit=D("4.00")))
        return _Outcome(status="FILLED", fill_credit=D("4.00"))


class _HealthyFill:
    """Stand-in ExecuteEntryAttempt for the happy path."""

    def __init__(self, comp) -> None:
        self._comp = comp

    async def attempt(self, *, day, scheduled, condor, gates, risk, stop, **kwargs):
        entry_id = f"{day}#{condor.entry_number}"
        self._comp.events.append(CondorFilled(entry_id=entry_id, net_credit=D("4.00")))
        return _Outcome(status="FILLED", fill_credit=D("4.00"))


def _runtime(comp) -> LiveRuntime:
    async def selector(when, n, config=None):
        return _condor(n), None

    async def gates_provider():
        return GATES_PASS

    return LiveRuntime(comp, selector=selector, market_gates=gates_provider)


def _skips(events):
    return [e for e in events if isinstance(e, EntrySkipped)]


def _fills(events):
    return [e for e in events if isinstance(e, CondorFilled)]


# --- a crash mid-ladder, before any fill ---------------------------------------

def test_attempt_crash_before_fill_alerts_and_journals_skip_exactly_once():
    comp = _Comp(FastClock(OPEN))
    comp.execute = _CrashBeforeFill()
    rt = _runtime(comp)

    async def scenario():
        try:
            await rt.run_day(DAY, [OPEN])
        except RuntimeError:
            pass  # the day task itself re-raises through the shield -- expected

    asyncio.run(scenario())

    assert _fills(comp.events) == []
    skips = _skips(comp.events)
    assert len(skips) == 1
    assert skips[0].date == DAY and skips[0].entry_number == 1
    assert skips[0].reason == "attempt_crashed:RuntimeError"

    critical = [c for c in comp.alerts.calls if c[0] == "critical"]
    assert len(critical) == 1
    assert "2026-07-14" in critical[0][1] and "entry_number=1" in critical[0][1]
    # no fill on the journal => the alert must SAY so (reviewer finding 2)
    assert "no position was taken" in critical[0][1]
    assert "AFTER fill" not in critical[0][1]
    assert "boom-mid-ladder" in critical[0][2]["error"]


# --- a crash AFTER CondorFilled: alert, but NEVER a skip -----------------------

def test_attempt_crash_after_condor_filled_alerts_but_never_journals_a_skip():
    async def crashing_on_filled(entry_id, condor, stop, fill_credit):
        raise RuntimeError("boom-in-protect-handoff")

    comp = _Comp(FastClock(OPEN), on_filled=crashing_on_filled)
    comp.execute = _FillsThenComp_OnFilledCrashes(comp)
    rt = _runtime(comp)

    async def scenario():
        try:
            await rt.run_day(DAY, [OPEN])
        except RuntimeError:
            pass

    asyncio.run(scenario())

    fills = _fills(comp.events)
    assert len(fills) == 1 and fills[0].entry_id == f"{DAY}#1"
    assert _skips(comp.events) == []   # never mislabel a filled entry as skipped

    critical = [c for c in comp.alerts.calls if c[0] == "critical"]
    assert len(critical) == 1
    # a fill exists => the DANGEROUS wording: the operator must be told the
    # position may be live without stops and what to do (reviewer finding 2)
    assert "crashed AFTER fill" in critical[0][1]
    assert "may be live without stops" in critical[0][1]
    assert "place a stop or flatten manually" in critical[0][1]
    assert "no position was taken" not in critical[0][1]
    assert "boom-in-protect-handoff" in critical[0][2]["error"]


# --- a healthy fill: neither an alert nor a skip -------------------------------

def test_healthy_fill_neither_alerts_nor_journals_a_skip():
    comp = _Comp(FastClock(OPEN))
    comp.execute = _HealthyFill(comp)
    rt = _runtime(comp)

    filled = asyncio.run(rt.run_day(DAY, [OPEN]))

    assert filled == 1
    assert comp.protected == [f"{DAY}#1"]
    assert _skips(comp.events) == []
    assert comp.alerts.calls == []
