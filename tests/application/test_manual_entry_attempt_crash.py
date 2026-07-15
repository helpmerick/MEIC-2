"""ENT-09/ENT-11 x RSK-06: the manual ▶ fire's fire-and-forget attempt task
must never die journal-silent — the SAME gap as the scheduled path's
2026-07-14 incident (entry journaled EntryWindowOpened + CondorProposed then
NOTHING), closed by the SAME shared helper (`alert_and_journal_crashed_attempt`,
application/attempt_crash.py). The manual path previously carried its own
independent, alert-only copy of the callback — the duplication is exactly how
the journal gap survived there, so these tests pin the manual path against
the one shared implementation:

  * a crash BEFORE any fill -> critical alert + EntrySkipped(reason=
    "attempt_crashed:<ExcType>"), exactly once, stamped with the press's
    ENT-09b floors.
  * a crash AFTER CondorFilled already journaled (mid the STP-01 `_on_filled`
    hand-off) -> alert, but NO EntrySkipped.
  * a healthy fill -> neither.
  * the ORPHAN case (the reason the callback exists at all): the awaiting
    request handler is CANCELLED (client disconnect) while the shielded
    attempt runs on, and the attempt then crashes — nobody is left awaiting
    it, so the done-callback is the only observer: alert + skip must still
    both land.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor
from meic.application.manual_entry import ManualEntry
from meic.application.persistent_state import PersistentState
from meic.domain.events import CondorFilled, EntrySkipped

NOW = datetime(2026, 7, 14, 10, 7, tzinfo=timezone.utc)
DAY = "2026-07-14"
ADHOC_N = 101  # ENT-11: the manual ad-hoc 101+ numbering lane


class _Clock:
    def now(self):
        return NOW

    async def wait_until(self, when):
        return None


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


class _Comp:
    """Minimal composition stand-in; `execute` is a per-test scripted double."""

    def __init__(self, *, on_filled=None) -> None:
        self.events: list = []
        self.clock = _Clock()
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
    """The ladder itself blows up — no CondorFilled is ever journaled."""

    def __init__(self, started: asyncio.Event | None = None,
                 release: asyncio.Event | None = None) -> None:
        self._started = started
        self._release = release

    async def attempt(self, **kwargs):
        if self._started is not None:
            self._started.set()
        if self._release is not None:
            await self._release.wait()   # the orphan test cancels the CALLER here
        raise RuntimeError("boom-mid-ladder")


class _FillsThenReturns:
    """Fills exactly like the real ExecuteEntryAttempt — journals CondorFilled
    BEFORE returning FILLED; any crash then happens in `_on_filled`."""

    def __init__(self, comp) -> None:
        self._comp = comp

    async def attempt(self, *, day, condor, **kwargs):
        entry_id = f"{day}#{condor.entry_number}"
        self._comp.events.append(CondorFilled(entry_id=entry_id, net_credit=D("4.00")))
        return _Outcome(status="FILLED", fill_credit=D("4.00"))


async def _gates():
    return GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                        flatten_in_progress=False, market_open=True, market_halted=False,
                        data_fresh=True, session_valid=True, buying_power_ok=True)


async def _selector(when, n, config=None, put_floor=None, call_floor=None):
    return Condor(entry_number=n, put_short=D("5990"), call_short=D("6060"),
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00")), None


def _manual(comp) -> ManualEntry:
    return ManualEntry(comp, _selector, _gates, day=lambda: DAY)


def _skips(events):
    return [e for e in events if isinstance(e, EntrySkipped)]


def _fills(events):
    return [e for e in events if isinstance(e, CondorFilled)]


# --- a crash mid-ladder, before any fill ---------------------------------------

def test_manual_crash_before_fill_alerts_and_journals_skip_with_floors():
    comp = _Comp()
    comp.execute = _CrashBeforeFill()
    manual = _manual(comp)

    async def scenario():
        with pytest.raises(RuntimeError):
            # the handler is still attached, so the crash ALSO propagates to
            # it through the shield — the callback's journal must not double up
            await manual.fire(press_id="p1", entry_number=ADHOC_N, row=None,
                              confirmed=True, put_floor=D("5950"))
        for _ in range(10):   # drain the done-callback
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert _fills(comp.events) == []
    skips = _skips(comp.events)
    assert len(skips) == 1
    assert skips[0].date == DAY and skips[0].entry_number == ADHOC_N
    assert skips[0].reason == "attempt_crashed:RuntimeError"
    assert skips[0].put_floor == D("5950")   # ENT-09b audit carried onto the crash skip
    assert skips[0].call_floor is None

    critical = [c for c in comp.alerts.calls if c[0] == "critical"]
    assert len(critical) == 1
    assert DAY in critical[0][1] and f"entry_number={ADHOC_N}" in critical[0][1]
    # no fill on the journal => the alert must SAY so (reviewer finding 2)
    assert "no position was taken" in critical[0][1]
    assert "AFTER fill" not in critical[0][1]
    assert "boom-mid-ladder" in critical[0][2]["error"]


# --- a crash AFTER CondorFilled: alert, but NEVER a skip -----------------------

def test_manual_crash_after_condor_filled_alerts_but_never_journals_a_skip():
    async def crashing_on_filled(entry_id, condor, stop, fill_credit):
        raise RuntimeError("boom-in-protect-handoff")

    comp = _Comp(on_filled=crashing_on_filled)
    comp.execute = _FillsThenReturns(comp)
    manual = _manual(comp)

    async def scenario():
        with pytest.raises(RuntimeError):
            await manual.fire(press_id="p1", entry_number=ADHOC_N, row=None, confirmed=True)
        for _ in range(10):
            await asyncio.sleep(0)

    asyncio.run(scenario())

    fills = _fills(comp.events)
    assert len(fills) == 1 and fills[0].entry_id == f"{DAY}#{ADHOC_N}"
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

def test_manual_healthy_fill_neither_alerts_nor_journals_a_skip():
    comp = _Comp()
    comp.execute = _FillsThenReturns(comp)
    manual = _manual(comp)

    async def scenario():
        out = await manual.fire(press_id="p1", entry_number=ADHOC_N, row=None, confirmed=True)
        for _ in range(10):
            await asyncio.sleep(0)
        return out

    out = asyncio.run(scenario())

    assert out["result"] == "filled" and out["entry_id"] == f"{DAY}#{ADHOC_N}"
    assert comp.protected == [f"{DAY}#{ADHOC_N}"]
    assert _skips(comp.events) == []
    assert comp.alerts.calls == []


# --- the ORPHAN case: the request handler is gone when the crash lands ---------

def test_manual_crash_after_the_awaiting_request_disconnects_still_alerts_and_journals():
    """The exact case the old alert-only callback's own comment named: the
    client disconnects (the handler task is cancelled), the shielded attempt
    runs on unobserved, then crashes. The done-callback is the ONLY observer
    left — before this fix it alerted but left the journal silent."""
    async def scenario():
        comp = _Comp()
        started = asyncio.Event()
        release = asyncio.Event()
        comp.execute = _CrashBeforeFill(started, release)
        manual = _manual(comp)

        handler = asyncio.create_task(
            manual.fire(press_id="p1", entry_number=ADHOC_N, row=None, confirmed=True))
        await started.wait()          # the attempt is in flight at the "broker"
        handler.cancel()              # the client disconnected
        await asyncio.sleep(0)
        release.set()                 # NOW the attempt crashes — nobody awaits it

        with pytest.raises(asyncio.CancelledError):
            await handler             # the request handler is gone — fine

        for _ in range(10):           # let the shielded task die + callback run
            await asyncio.sleep(0)
        return comp

    comp = asyncio.run(scenario())

    assert _fills(comp.events) == []
    skips = _skips(comp.events)
    assert len(skips) == 1
    assert skips[0].entry_number == ADHOC_N
    assert skips[0].reason == "attempt_crashed:RuntimeError"

    critical = [c for c in comp.alerts.calls if c[0] == "critical"]
    assert len(critical) == 1
    assert "no position was taken" in critical[0][1]   # orphan crash, no fill
    assert "boom-mid-ladder" in critical[0][2]["error"]
