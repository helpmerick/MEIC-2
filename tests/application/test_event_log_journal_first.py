"""REC-01 — DurableEventLog ordering, pinned (v1.74 fix batch).

Ratified ordering: JOURNAL-FIRST. An event is durably written to the journal
BEFORE it is appended to the in-memory list (before any in-memory state or
actor can observe it); a journal failure RAISES and the in-memory list is
left UNCHANGED. The rejected alternative (in-memory-first, found live) left a
"lie window": a journal failure there still let the in-memory event be acted
upon, so a restart replayed a SHORTER, different history than the one the
process had just acted on.

These tests pin (a) the ordering itself, (b) the failure contract (raise +
no in-memory mutation), (c) that `extend` never lets the two stores diverge
mid-batch, and (d) full replay-from-journal identity across a realistic event
mix (distinct from TC-REC-01's `SqliteEventStore`-based replay test — this
exercises the actual `DurableEventLog`/`EventJournal` write-through pair).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from meic.adapters.persistence.event_store import EventJournal
from meic.application.event_log import DurableEventLog
from meic.domain.events import CondorFilled, DayArmed, LongSold, ShortStopped, SideExpired


class _OrderRecordingJournal:
    """A fake journal that snapshots the in-memory list's length AT THE
    MOMENT its own append() runs -- the only way to observe ordering from
    the outside without reaching into DurableEventLog's internals."""

    def __init__(self, backing_list: list) -> None:
        self._backing = backing_list
        self.lengths_at_append: list[int] = []

    def append(self, event) -> None:
        self.lengths_at_append.append(len(self._backing))


class _FailingJournal:
    def __init__(self, fail_on: int = 1) -> None:
        self.fail_on = fail_on
        self.calls = 0

    def append(self, event) -> None:
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("journal write failed (disk full)")


def test_rec01_append_journals_before_in_memory_mutation():
    """The journal must see the in-memory list still at its PRE-append
    length when it receives the event -- proof the write-through happens
    before the list is mutated, not after (the ordering this fix reverses)."""
    log = DurableEventLog(journal=None)  # placeholder; swapped below
    journal = _OrderRecordingJournal(log)
    log._journal = journal

    log.append(DayArmed(date="2026-07-15", entry_count=1))
    assert journal.lengths_at_append == [0], (
        "journal.append() must run while the in-memory list is still empty")
    assert len(log) == 1  # only AFTER the journal succeeded


def test_rec01_journal_failure_raises_and_leaves_memory_unchanged():
    """FAIL-FIRST (pre-fix behaviour): the old in-memory-first order mutated
    `log` before the journal ever ran, so a raise here still left the event
    sitting in memory -- the exact "lie window" REC-01 rejects. Fixed: a
    raise leaves the in-memory list byte-identical to before the call."""
    log = DurableEventLog(journal=_FailingJournal(fail_on=1))
    with pytest.raises(RuntimeError, match="journal write failed"):
        log.append(DayArmed(date="2026-07-15", entry_count=1))
    assert list(log) == [], "a journal failure must never leave an event in memory"


def test_rec01_extend_partial_failure_keeps_journal_and_memory_in_sync():
    """A multi-event extend() whose journal fails partway must not journal
    item 3 while only items 1-2 land in memory (or vice versa) -- per-item
    interleaving keeps the two stores never more than one item apart, and
    the FAILED item lands in neither."""
    journal = _FailingJournal(fail_on=3)  # succeeds for items 1 and 2, fails on 3
    log = DurableEventLog(journal=journal)
    events = [
        DayArmed(date="2026-07-15", entry_count=1),
        DayArmed(date="2026-07-15", entry_count=2),
        DayArmed(date="2026-07-15", entry_count=3),
    ]
    with pytest.raises(RuntimeError, match="journal write failed"):
        log.extend(events)

    assert len(log) == 2, "only the two journal-confirmed items may reach memory"
    assert [e.entry_count for e in log] == [1, 2]
    assert journal.calls == 3, "the journal saw exactly the two successes plus the failing third"


def test_rec01_successful_extend_lands_every_item_in_order():
    """Sanity: the happy path is unaffected -- extend still lands every item,
    in order, in both stores."""
    class _RecordingJournal:
        def __init__(self):
            self.seen = []

        def append(self, event):
            self.seen.append(event)

    journal = _RecordingJournal()
    log = DurableEventLog(journal=journal)
    events = [DayArmed(date="2026-07-15", entry_count=n) for n in (1, 2, 3)]
    log.extend(events)
    assert list(log) == events
    assert journal.seen == events


# --- review finding C: the crash-callback's double fault is VISIBLE ---------

def test_rec01_crash_callback_journal_failure_alerts_and_never_propagates():
    """PIN (v1.74 review finding C): with journal-first, `comp.events.append`
    inside `attempt_crash`'s done-callback can now RAISE on a journal write
    failure -- the skip is then recorded NOWHERE (the journal refused it,
    memory never got it). The callback must still never propagate (it must
    not kill the event loop), but the double fault must reach the panel: an
    ALERT is emitted in addition to stderr, not a stderr-only print."""
    import asyncio
    import types

    from meic.application.attempt_crash import alert_and_journal_crashed_attempt

    class _Alerts:
        def __init__(self):
            self.calls = []

        def alert(self, level, message, **ctx):
            self.calls.append((level, message, ctx))

    class _JournalDeadEvents(list):
        """An empty in-memory log whose append raises -- the exact surface a
        DurableEventLog presents when its journal write fails (REC-01:
        raise, memory unchanged)."""

        def append(self, item):
            raise RuntimeError("journal write failed (disk full)")

    async def scenario():
        async def _boom():
            raise RuntimeError("attempt crashed")

        task = asyncio.create_task(_boom())
        await asyncio.sleep(0)
        assert task.done() and task.exception() is not None

        alerts = _Alerts()
        comp = types.SimpleNamespace(events=_JournalDeadEvents(), alerts=alerts)
        cb = alert_and_journal_crashed_attempt(comp, "2026-07-15", 1)
        cb(task)  # must NOT raise -- the callback never kills the loop

        # two alerts: the crash alert itself, then the double-fault alert.
        assert len(alerts.calls) == 2
        crash_level, crash_msg, _ = alerts.calls[0]
        assert crash_level == "critical" and "entry attempt crashed" in crash_msg
        fault_level, fault_msg, fault_ctx = alerts.calls[1]
        assert fault_level == "critical"
        assert "journaled NOWHERE" in fault_msg and "REC-01" in fault_msg
        assert "journal write failed" in fault_ctx["error"]

    asyncio.run(scenario())


def test_rec01_crash_callback_survives_even_a_dead_alert_sink():
    """The double-fault alert itself is guarded: journal dead AND alert sink
    dead must still not propagate out of the callback (stderr remains the
    backstop of last resort)."""
    import asyncio
    import types

    from meic.application.attempt_crash import alert_and_journal_crashed_attempt

    class _DeadAlerts:
        def alert(self, level, message, **ctx):
            raise RuntimeError("alert sink down")

    class _JournalDeadEvents(list):
        def append(self, item):
            raise RuntimeError("journal write failed")

    async def scenario():
        async def _boom():
            raise RuntimeError("attempt crashed")

        task = asyncio.create_task(_boom())
        await asyncio.sleep(0)

        comp = types.SimpleNamespace(events=_JournalDeadEvents(), alerts=_DeadAlerts())
        alert_and_journal_crashed_attempt(comp, "2026-07-15", 1)(task)  # must not raise

    asyncio.run(scenario())


# --- full replay-identity regression, realistic event mix -------------------

def _realistic_event_mix() -> list:
    """A plausible day: two entries, one whipsawed (both stops+LEX sales),
    one expired both sides -- the same shape TC-REC-01 exercises, but driven
    through the real DurableEventLog/EventJournal write-through pair rather
    than SqliteEventStore."""
    return [
        DayArmed(date="2026-07-15", entry_count=2),
        CondorFilled(entry_id="2026-07-15#1", net_credit=D("4.00")),
        ShortStopped(entry_id="2026-07-15#1", side="PUT", fill=D("3.80"), slippage=D("0.20")),
        LongSold(entry_id="2026-07-15#1", side="PUT", recovery=D("0.15")),
        ShortStopped(entry_id="2026-07-15#1", side="CALL", fill=D("3.75"), slippage=D("0.25")),
        LongSold(entry_id="2026-07-15#1", side="CALL", recovery=D("0.10")),
        CondorFilled(entry_id="2026-07-15#2", net_credit=D("3.50")),
        SideExpired(entry_id="2026-07-15#2", side="PUT"),
        SideExpired(entry_id="2026-07-15#2", side="CALL"),
    ]


def test_rec01_full_replay_identity_through_real_event_journal(tmp_path):
    """Append a realistic mix through the REAL journal-backed DurableEventLog,
    then reopen a fresh EventJournal on the same file: replay-from-journal
    must equal the in-memory state exactly, in both directions (REC-01's
    'no divergence either direction' pin)."""
    db_path = tmp_path / "state.db"
    journal = EventJournal(db_path)
    log = DurableEventLog(journal=journal)

    for event in _realistic_event_mix():
        log.append(event)

    in_memory_types = [type(e).__name__ for e in log]

    reopened = EventJournal(db_path)
    replayed = reopened.load()
    replayed_types = [type(e).__name__ for e in replayed]

    assert replayed_types == in_memory_types
    assert len(replayed) == len(log)
    for original, reloaded in zip(log, replayed):
        assert original == reloaded, f"{type(original).__name__} diverged on replay"

    journal.close()
    reopened.close()
