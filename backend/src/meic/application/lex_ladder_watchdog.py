"""LEX ladder invariant watchdog (2026-07-14) — the CLASS-level fix for the
2026-07-10 incident: `ShortStopped(entry_id="2026-07-10#1", side="CALL")` was
journaled and then NOTHING -- no `LongSaleStarted`, no `LexOrderPlaced`, no
`LongSold`, no `SideClosed`. LEX-07 requires the long ALWAYS be sold after a
stop-out; the ladder never ran, and nothing noticed for three days because a
component that is never invoked emits no WRONG events -- it emits NO events,
and nothing that inspects events for mistakes can see an absence.

This module is the antidote: it asserts an INVARIANT over the JOURNAL itself
--  "once a side is ShortStopped (a genuine stop-out, not DCY-03's deliberate
decay leave-to-expire), a LEX ladder MUST have started within a bounded grace
window" -- rather than hooking the LEX code path. A hook inside a component
that never runs, never runs either; a fold over the event log fires
regardless of whether the LEX service is wired, reachable, or alive at all.

DCY-03 (spec/01-strategy-rules.md): after a decay buyback the side's long is
DELIBERATELY left to expire -- LEX-07's always-sell is for stop-outs, not
decay closes. `ShortStopped.initiator == "decay"` is therefore a LEGAL
exception, checked directly (not merely inferred from the absence of a later
`EntryClosed`) because `decay_watcher.complete()` / `stop_fill_watch.py`'s
decay path append `ShortStopped(initiator="decay")` and
`EntryClosed(initiator="decay")` as two separate list appends -- a crash
between them would leave the decay `ShortStopped` journaled alone, and this
watchdog must still recognise it as legal, not alarm on it.

Purely a DETECTOR (CLS-02): it only reads `events` and calls `alerts.alert`.
It never places, cancels, or modifies any broker order, and carries no
broker/order-action capability whatsoever.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from meic.domain.events import (
    EntryClosed,
    Event,
    LongSaleStarted,
    LongSold,
    ShortStopped,
    SideClosed,
)


def _pending_ladder_starts(events: list[Event]) -> set[tuple[str, str]]:
    """LEX-07: every (entry_id, side) whose short was stopped by a genuine
    STOP-OUT and has NOT yet started a LEX ladder (`LongSaleStarted`), nor
    reached a terminal LEX/close state (`LongSold`/`SideClosed`), nor been
    closed at the entry level by any other path (`EntryClosed` -- CLS or
    otherwise; see `close_entry.py`, the only writer of that event, which
    fully disposes of an entry's positions outside the ladder).

    Pure fold, no timestamps involved -- presence/absence only. Ordering
    within the log is not re-verified here: the caller (the watchdog's own
    `observe`) is what turns "still pending" into a bounded-grace-window
    decision using wall time it tracks itself."""
    stop_initiator: dict[tuple[str, str], str] = {}
    started: set[tuple[str, str]] = set()
    terminal: set[tuple[str, str]] = set()
    closed_entries: set[str] = set()

    for e in events:
        if isinstance(e, ShortStopped):
            stop_initiator[(e.entry_id, e.side)] = e.initiator
        elif isinstance(e, LongSaleStarted):
            started.add((e.entry_id, e.side))
        elif isinstance(e, (LongSold, SideClosed)):
            terminal.add((e.entry_id, e.side))
        elif isinstance(e, EntryClosed):
            closed_entries.add(e.entry_id)

    pending: set[tuple[str, str]] = set()
    for key, initiator in stop_initiator.items():
        entry_id, _side = key
        if initiator == "decay":  # DCY-03: deliberately left to expire, legal
            continue
        if key in started or key in terminal:
            continue
        if entry_id in closed_entries:
            continue
        pending.add(key)
    return pending


@dataclass
class LexLadderWatchdog:
    """LEX-07 invariant watchdog. `observe()` is called once per live
    management tick (see adapters/api/server.py `_lex_ladder_watchdog_pass` /
    `_run_lex_ladder_watchdog_loop`) with the CURRENT journal and wall clock.

    Grace window: a ladder legitimately takes a few seconds to start, so a
    side is only alerted once it has been seen pending for
    `grace_seconds` of REAL wall time -- tracked here (`_first_seen`), since
    domain events do not themselves carry a wall-clock field. One alert ever
    per (entry_id, side) -- `_alerted`, the same in-memory once-per-key dedup
    convention as `ReportReconciler._unattributable_alerted`
    (application/report_reconciler.py)."""

    alerts: object
    grace_seconds: Decimal = Decimal("60")
    _first_seen: dict[tuple[str, str], datetime] = field(default_factory=dict)
    _alerted: set = field(default_factory=set)

    def observe(self, events: list[Event], *, now: datetime) -> None:
        pending = _pending_ladder_starts(events)

        # A key that left the pending frame (ladder started, terminal state
        # reached, or the entry closed some other way) has nothing left to
        # track -- drop its bookkeeping so it never inherits a stale
        # first-seen baseline if it somehow reappeared.
        for key in set(self._first_seen) - pending:
            self._first_seen.pop(key, None)

        for key in pending:
            if key in self._alerted:
                continue  # one alert per (entry, side), never per tick
            first = self._first_seen.setdefault(key, now)
            elapsed = (now - first).total_seconds()
            if elapsed >= float(self.grace_seconds):
                self._alerted.add(key)
                entry_id, side = key
                self.alerts.alert(
                    "critical",
                    "LEX-07: side ShortStopped but no LEX ladder started within the grace window",
                    entry_id=entry_id, side=side)
