"""CalendarStore — CAL-01..08 operator-triggered mutation surface (doc 11).

Thin façade over the SHARED event log: every mutation appends a domain event
(domain/events.py's CAL-* events) and every read replays the pure fold
(domain/trading_calendar.py). The journal IS the durable store (REC-07's
v1.71 extension) -- there is no second persistence path to keep in sync, and
a reboot restores tags/rules exactly by construction (replay), mirroring how
`domain.projection.fold` already backs every entry/position projection.

CAL-07's fail-open polarity (the one deliberate exception in this codebase --
quoted verbatim below) is enforced HERE, at the boundary the ENT-03/ENT-06
gate chain actually calls through: `label_for_day` never raises and never
lets an empty/unimported/unreadable calendar read as anything but "no tag".
"""
from __future__ import annotations

import logging
from typing import Any

from meic.domain.events import (
    CalendarEventsImported,
    ManualFireBlackoutAcknowledged,
    NoTradeTagRemoved,
    NoTradeTagSet,
    StandingCategoryRuleRemoved,
    StandingCategoryRuleSet,
)
from meic.domain.trading_calendar import (
    KNOWN_CATEGORIES,
    CalendarState,
    CategoryStaleness,
    effective_tags,
    fold,
)
from meic.domain.trading_calendar import label_for_day as _label_for_day
from meic.domain.trading_calendar import staleness as _staleness


logger = logging.getLogger(__name__)


class UnknownCalendarCategory(ValueError):
    """CAL-01: never silently accept a third, unspecced tier."""


class CalendarStore:
    def __init__(self, events: list, clock) -> None:
        self._events = events
        self._clock = clock

    def state(self) -> CalendarState:
        return fold(self._events)

    # --- CAL-01 import -----------------------------------------------------
    def import_events(self, *, category: str, dates: list[str],
                       labels: dict[str, str] | None = None,
                       source: str = "pasted_table") -> CalendarEventsImported:
        """Operator-triggered (endpoint-gated, same auth as every mutating
        route). `dates` never fabricated by this layer — exactly what the
        caller supplied (the pasted table, or a future read-only fetch's own
        result), deduplicated and sorted for a stable, replayable event."""
        if category not in KNOWN_CATEGORIES:
            raise UnknownCalendarCategory(category)
        labels = labels or {}
        unique_dates = sorted(set(dates))
        ev = CalendarEventsImported(
            category=category, dates=tuple(unique_dates),
            labels=tuple(labels.get(d, "") for d in unique_dates),
            imported_at=self._clock.now().isoformat(), source=source)
        self._events.append(ev)
        return ev

    # --- CAL-03 manual tags --------------------------------------------------
    def tag(self, day: str, label: str) -> NoTradeTagSet:
        ev = NoTradeTagSet(day=day, label=label, origin="manual")
        self._events.append(ev)
        return ev

    def untag(self, day: str) -> NoTradeTagRemoved:
        ev = NoTradeTagRemoved(day=day)
        self._events.append(ev)
        return ev

    # --- CAL-04 standing category rules --------------------------------------
    def set_standing_rule(self, category: str, label: str | None = None) -> StandingCategoryRuleSet:
        if category not in KNOWN_CATEGORIES:
            raise UnknownCalendarCategory(category)
        ev = StandingCategoryRuleSet(category=category, label=label)
        self._events.append(ev)
        return ev

    def remove_standing_rule(self, category: str) -> StandingCategoryRuleRemoved:
        ev = StandingCategoryRuleRemoved(category=category)
        self._events.append(ev)
        return ev

    # --- CAL-05 gate read (ENT-06, through entry_gates.FilterSnapshot) -------
    def label_for_day(self, day: str) -> str | None:
        """CAL-07 (quoted): "an empty or unimported calendar blocks nothing
        ... Unlike the halt gate (DAT-04a), blackouts are operator-AUTHORED
        additions". This is the ONE gate input in this codebase that fails
        OPEN on error, by deliberate spec ruling -- never generalize this
        try/except shape to any other gate input. A store that cannot even
        fold its own log (corrupt state, a bug in a future category) must
        read as untagged, not as blocked -- the bot traded correctly before
        this feature existed, and a defect in it must not stop trading.

        Failing open is RULED; failing SILENT is not (final-review finding 2,
        2026-07-15): a fold bug swallowed here would disable every blackout
        forever with no trace, so the exception is logged (with traceback)
        before the fail-open None -- the operator gets a line in the log,
        the gate still gets its ruled answer."""
        try:
            return _label_for_day(self.state(), day)
        except Exception:
            logger.exception(
                "CAL-07 fail-open: calendar store unreadable for %s -- "
                "treating as untagged (blackouts NOT enforced this read)", day)
            return None

    def tags(self) -> dict[str, Any]:
        """CAL-08 read model (backend half): every currently-tagged day."""
        return effective_tags(self.state())

    # --- CAL-06 manual-fire acknowledgment -----------------------------------
    def acknowledge_override(self, day: str, label: str) -> ManualFireBlackoutAcknowledged:
        ev = ManualFireBlackoutAcknowledged(day=day, label=label, at=self._clock.now().isoformat())
        self._events.append(ev)
        return ev

    # --- CAL-02 staleness (display-only, never blocking) ---------------------
    def staleness_report(self, *, stale_after_days: int) -> dict[str, CategoryStaleness]:
        return _staleness(self.state(), now=self._clock.now(), stale_after_days=stale_after_days)
