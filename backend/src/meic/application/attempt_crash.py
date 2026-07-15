"""The ONE shared done-callback for a fire-and-forget entry-attempt task —
ENT-10/RSK-06 (scheduled path, composition/live_runtime.py) and ENT-09/ENT-11
(manual ▶ path, application/manual_entry.py).

2026-07-14 production incident: scheduled entry 2026-07-14#2 journaled
EntryWindowOpened + CondorProposed and then NOTHING — no fill, no skip. The
attempt task had crashed (the sqlite shared-connection race hotfixed
alongside this) and died with its exception never retrieved ("Task exception
was never retrieved" alongside it in stderr), so nothing downstream ever knew
entry #2 was dead.

Both entry paths run their attempt as a shielded `asyncio.ensure_future`
task — deliberately fire-and-forget, so an ENT-10(3) disarm/cancel (or a
disconnected HTTP request on the manual path) can never orphan a live resting
order mid-ladder. The price of that design is exactly this callback: it is
the ONLY thing guaranteed to observe the task's outcome. It used to exist as
two independent copies (one per path), and only the alert half — the
journal-silent gap above survived precisely BECAUSE of the duplication, so
this module replaces both copies with one implementation.
"""
from __future__ import annotations

import sys
from decimal import Decimal

from meic.domain.events import CondorFilled, EntrySkipped


def entry_outcome(events, day: str, entry_number: int, entry_id: str) -> tuple[bool, bool]:
    """(filled, skipped) for this entry, from the log — a `CondorFilled` for
    its entry_id, and/or an `EntrySkipped` for its (day, entry_number).
    Scanned fresh from `comp.events` at done-callback time (never cached):
    the attempt can crash BEFORE the fill posts its own event, or AFTER
    (e.g. mid `_on_filled` STP-01 hand-off) — the caller must never
    double-journal a skip, never mislabel an entry that actually filled as
    skipped, and must word its alert DIFFERENTLY when a fill exists (the
    position may be live without stops — the dangerous case)."""
    filled = skipped = False
    for e in events:
        if isinstance(e, CondorFilled) and e.entry_id == entry_id:
            filled = True
        elif isinstance(e, EntrySkipped) and e.date == day and e.entry_number == entry_number:
            skipped = True
    return filled, skipped


def entry_already_resolved(events, day: str, entry_number: int, entry_id: str) -> bool:
    """True iff the log already carries a terminal outcome for this entry
    (see `entry_outcome`)."""
    filled, skipped = entry_outcome(events, day, entry_number, entry_id)
    return filled or skipped


def alert_and_journal_crashed_attempt(comp, day: str, entry_number: int, *,
                                      put_floor: Decimal | None = None,
                                      call_floor: Decimal | None = None):
    """Build the done-callback for a shielded, fire-and-forget attempt task.

    `entry_number` is whatever lane the caller fires in — a scheduled row's
    durable ENT-10(4) id, or the manual path's ENT-11 ad-hoc 101+ number; the
    entry_id it derives (`{day}#{entry_number}`) is the SAME shape both lanes
    already stamp on their events, so the resolved-check keys match.

    `put_floor`/`call_floor` (ENT-09b v1.57): the manual path stamps the
    press's floors on every `EntrySkipped` it journals, for audit — a crash
    skip must carry them too. `None`/`None` (the default) is every scheduled
    caller and every floorless press.

    `task.exception()` is called UNCONDITIONALLY first (once we know the
    task was not cancelled) — that call is what marks the exception
    retrieved, so it must never be gated behind the alert/journal logic
    that follows it:
      (a) RSK-06 critical alert naming day, entry_number and repr(exc). The
          wording DISTINGUISHES the dangerous case: when the journal shows a
          `CondorFilled` (the crash landed AFTER the fill, e.g. mid the
          STP-01 protect hand-off), the position may be live WITHOUT stops —
          the alert says so and tells the operator what to do. When no fill
          exists, the alert says no position was taken.
      (b) EntrySkipped(reason=f"attempt_crashed:{ExcType}") journaled IFF
          `entry_outcome` says this entry has no CondorFilled and no
          EntrySkipped yet — re-derived from comp.events at callback time,
          never assumed from the crash's timing.

    The whole callback is wrapped in a bare except: a done-callback that
    itself raises would be logged by asyncio as an unhandled exception in
    the callback machinery, not re-raised into the event loop — but this
    guard removes any dependence on that backstop, matching the "must not
    kill the loop" requirement literally.
    """
    entry_id = f"{day}#{entry_number}"

    def _cb(task) -> None:
        try:
            if task.cancelled():
                return
            exc = task.exception()  # retrieval — must run before anything else can fail
            if exc is None:
                return
            filled, skipped = entry_outcome(comp.events, day, entry_number, entry_id)
            alerts = getattr(comp, "alerts", None)
            if alerts is not None:
                if filled:
                    message = (f"entry attempt crashed AFTER fill: day={day} "
                               f"entry_number={entry_number} — the condor may be live "
                               "without stops; check the journal for CondorFilled and "
                               "place a stop or flatten manually")
                else:
                    message = (f"entry attempt crashed: day={day} "
                               f"entry_number={entry_number} — no position was taken")
                alerts.alert("critical", message, error=repr(exc))
            if not filled and not skipped:
                comp.events.append(EntrySkipped(
                    date=day, entry_number=entry_number,
                    reason=f"attempt_crashed:{type(exc).__name__}",
                    put_floor=put_floor, call_floor=call_floor))
        except Exception as cb_exc:  # noqa: BLE001 — a broken callback must not kill the loop
            # REC-01 (v1.74 review finding C): with the journal-first reorder,
            # `comp.events.append` above can now RAISE on a journal write
            # failure — landing here. A stderr-only print would lose the skip
            # from BOTH stores (the journal refused it, memory never got it)
            # invisibly: a double fault (crashed attempt + dead journal) the
            # operator would never see. Still never propagate (the callback
            # must not kill the loop), but ALERT so it reaches the panel —
            # critical, matching this callback's own alert level: the entry's
            # terminal outcome may now be recorded NOWHERE. The alert call is
            # itself guarded (a dead alert sink must not kill the loop
            # either); stderr remains the backstop of last resort.
            try:
                alerts = getattr(comp, "alerts", None)
                if alerts is not None:
                    alerts.alert(
                        "critical",
                        f"crash-callback for {entry_id} itself failed — the "
                        "attempt_crashed skip may be journaled NOWHERE "
                        "(possible journal write failure, REC-01); verify the "
                        "event log and this entry's state by hand",
                        error=repr(cb_exc))
            except Exception:  # noqa: BLE001
                pass
            print(f"alert_and_journal_crashed_attempt: callback itself failed for "
                  f"{entry_id}: {cb_exc!r}", file=sys.stderr)
    return _cb
