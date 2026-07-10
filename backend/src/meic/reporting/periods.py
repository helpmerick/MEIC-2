"""RPT-01 period buckets (Today / day / month / year / all-time) -- pure
filtering over a trading-day set and the event log. This module holds no
clock and no I/O: the caller (adapters/api/reports.py) supplies "today" (the
ET calendar day, DAY-03) since a reporting module has no business reading a
clock of its own (doc 10 Principle 1).
"""
from __future__ import annotations

from meic.domain.events import (
    CondorFilled,
    CondorProposed,
    CorrectionRecord,
    DayArmed,
    DayBrokerConfirmed,
    DayCompleted,
    EntryClosed,
    EntryCompleted,
    EntryMarkSample,
    EntrySkipped,
    Event,
    LongSold,
    ShortStopped,
    SideClosed,
    SideExpired,
)

# Events keyed by an entry_id ("{day}#{n}") -- day membership comes from the
# id's own prefix (folds.entry_day's convention, mirrored here to avoid an
# import cycle: periods.py is a peer of folds.py, not a consumer of it).
_ENTRY_SCOPED = (
    CondorProposed, CondorFilled, ShortStopped, LongSold,
    SideClosed, SideExpired, EntryClosed, EntryCompleted, EntryMarkSample,
)
# Events keyed directly by a `date`/`day` field.
_DAY_SCOPED = (DayArmed, EntrySkipped, DayCompleted, DayBrokerConfirmed, CorrectionRecord)


def _entry_day(entry_id: str) -> str:
    return entry_id.split("#", 1)[0]


def resolve_period(
    days: tuple[str, ...],
    *,
    period: str | None = None,
    day: str | None = None,
    month: str | None = None,
    year: str | None = None,
    today: str | None = None,
) -> tuple[str, ...]:
    """RPT-01's five buckets. Exactly one of `day` (YYYY-MM-DD), `month`
    (YYYY-MM), `year` (YYYY), or `period` ("today"|"all") narrows the
    qualifying trading-day set; nothing narrows it -> "all". ISO date strings
    sort and prefix-match lexicographically, so no date parsing is needed."""
    if day:
        return tuple(d for d in days if d == day)
    if month:
        return tuple(d for d in days if d.startswith(month))
    if year:
        return tuple(d for d in days if d.startswith(year))
    if period == "today" and today:
        return tuple(d for d in days if d == today)
    return tuple(days)


def scope_events(events: list[Event], days: tuple[str, ...]) -> list[Event]:
    """Every event whose day (entry-id prefix, or its own `date` field) is in
    `days`. An event this module doesn't recognize as day- or entry-scoped
    (e.g. ModeSwitchStaged) passes through unfiltered -- it isn't a period
    concept at all, and reporting.folds already ignores what it doesn't fold.
    """
    day_set = set(days)
    out: list[Event] = []
    for e in events:
        if isinstance(e, _DAY_SCOPED):
            if getattr(e, "date", None) in day_set:
                out.append(e)
        elif isinstance(e, _ENTRY_SCOPED):
            if _entry_day(e.entry_id) in day_set:
                out.append(e)
        else:
            out.append(e)
    return out
