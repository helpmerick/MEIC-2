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
import re
from datetime import date as _date
from typing import Any

from meic.domain.events import (
    CalendarEventsImported,
    CalendarRefreshRejected,
    CalendarRefreshSucceeded,
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


class InvalidCalendarRefreshData(ValueError):
    """CAL-09 (review fix 4, 2026-07-16): a date or label a SOURCE scraped
    that fails the store's own validation gate -- refused BEFORE anything is
    appended, so no future source adapter can journal unbounded/malformed
    scraped text (the same bounds adapters/api/app.py's `_cal_day`/
    `_cal_label` enforce on operator input, applied here at the store
    boundary the sources cannot bypass)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# Same shape rules as adapters/api/app.py's `_cal_day`/`_cal_label`
# (deliberately DUPLICATED per the review ruling rather than imported: the
# app-layer versions raise HTTPException, an adapters-layer type this
# application module must not depend on; the CONSTANTS are the contract --
# strict ASCII YYYY-MM-DD naming a real date, and a bounded printable
# single-line label, rejected never truncated).
_REFRESH_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_REFRESH_LABEL_MAX = 64


def _validate_refresh_day(raw) -> str:
    # The offending value's repr is BOUNDED (`:.80`) in both messages: this
    # reason string flows into the journaled CalendarRefreshRejected.reason
    # via the coordinator's invalid_data path -- an unbounded echo of the
    # scraped garbage would smuggle in exactly the unbounded text this gate
    # exists to keep out of the journal.
    day = raw if isinstance(raw, str) else ""
    if not _REFRESH_DAY_RE.fullmatch(day):
        raise InvalidCalendarRefreshData(f"invalid_day:{raw!r:.80}")
    try:
        _date.fromisoformat(day)
    except ValueError:
        raise InvalidCalendarRefreshData(f"invalid_day:{raw!r:.80}")
    return day


def _validate_refresh_label(raw) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > _REFRESH_LABEL_MAX \
            or not raw.isprintable():
        raise InvalidCalendarRefreshData(f"invalid_label:{raw!r:.80}")
    return raw


class CalendarStore:
    def __init__(self, events: list, clock) -> None:
        self._events = events
        self._clock = clock

    def state(self) -> CalendarState:
        return fold(self._events)

    @property
    def events(self) -> list:
        """CAL-09: read access to the SAME journal every mutation above
        appends to -- lets application/calendar_refresh.py compute the
        consecutive-failure streak (domain/trading_calendar.py) and the
        once-per-day gate without a second event list to keep in sync."""
        return self._events

    @property
    def clock(self):
        """CAL-09: the SAME clock every `record_*`/`tag`/`import_events` call
        already reads internally -- exposed so a caller building a SECOND
        collaborator against this store's own journal (e.g.
        `CalendarRefreshCoordinator`) shares one clock instance rather than
        reaching into a private attribute."""
        return self._clock

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

    # --- CAL-09 daily auto-refresh (v1.77) -----------------------------------
    def record_refresh_success(self, *, category: str, dates: list[str],
                                labels: dict[str, str] | None = None,
                                source: str) -> CalendarRefreshSucceeded:
        """CAL-09 rule 2: ADDITIVE merge -- the union of every date `category`
        has EVER successfully carried, never a replace. `dates` here is only
        THIS fetch's own result; the merge against the prior state (and the
        added/disputed diff rule 3 demands) is computed HERE, once, so both
        the fold (domain/trading_calendar.py) and every caller see the exact
        same numbers the event itself carries -- no second computation to
        drift from this one.

        Review fix 4 (2026-07-16): every date and label is validated HERE,
        before ANYTHING is appended -- strict YYYY-MM-DD naming a real date,
        bounded printable single-line labels (the same rules `_cal_day`/
        `_cal_label` apply to operator input in adapters/api/app.py) -- so
        no source adapter, present or future, can journal unbounded scraped
        text. Raises `InvalidCalendarRefreshData` with nothing written:
        an all-or-nothing gate, the same reject-whole shape as CAL-09's own
        rule 1."""
        if category not in KNOWN_CATEGORIES:
            raise UnknownCalendarCategory(category)
        dates = [_validate_refresh_day(d) for d in dates]
        labels = {_validate_refresh_day(k): _validate_refresh_label(v)
                  for k, v in (labels or {}).items()}
        prior = self.state().imports.get(category)
        prior_dates = prior.dates if prior is not None else frozenset()
        prior_labels = dict(prior.labels) if prior is not None else {}
        new_dates = frozenset(dates)
        merged_dates = sorted(prior_dates | new_dates)
        merged_labels = {**prior_labels, **{d: lbl for d, lbl in (labels or {}).items() if lbl}}
        ev = CalendarRefreshSucceeded(
            category=category, dates=tuple(merged_dates),
            labels=tuple(merged_labels.get(d, "") for d in merged_dates),
            added_dates=tuple(sorted(new_dates - prior_dates)),
            disputed_dates=tuple(sorted(prior_dates - new_dates)),
            source=source, fetched_at=self._clock.now().isoformat())
        self._events.append(ev)
        return ev

    def record_refresh_rejected(self, *, category: str, reason: str,
                                 source: str) -> CalendarRefreshRejected:
        """CAL-09 rule 1: journals the REJECTION only -- never mutates
        `imports` (no other event is appended here), so existing data is
        byte-identical to before this call by construction."""
        if category not in KNOWN_CATEGORIES:
            raise UnknownCalendarCategory(category)
        ev = CalendarRefreshRejected(category=category, reason=reason, source=source,
                                      checked_at=self._clock.now().isoformat())
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
