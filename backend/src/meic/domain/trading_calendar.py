"""Trading calendar projection — CAL-01..04/07 (pure, doc 11).

A deterministic fold over the CAL-* events (domain/events.py), mirroring
`domain/projection.py`'s day-state fold (REC-01): replaying the same events
always yields an equal `CalendarState`, so REC-07's v1.71 inventory
extension ("calendar NO-TRADE tags and standing category rules ... restored
exactly on any boot") needs no new persistence path — the shared event
journal already IS the durable store; a reboot just replays it.

Two tiers (CAL-01, "honestly separated"): TIER_1 is the official,
published-in-advance schedules (FOMC/CPI/NFP/PPI/PCE/GDP); TIER_2 is the
best-effort Fed-speaker feed, display-only in trust terms. Both are
taggable; neither is ever silently guessed — a day with no import simply has
no entry in `CalendarState.imports`, never a fabricated one.

Auto-tagging (CAL-04) is computed HERE, at read time, from the CURRENT
import + the standing rule — never written back as individual tag events.
That is what makes "later-imported events of the category" auto-tag for
free: the next import just changes what `effective_tags` folds over, with
no backfill pass required. An auto-tag individually removed
(`NoTradeTagRemoved`) is tracked in `removed_days` and stays suppressed even
across a rule removal/re-add or a fresh import naming that same date again.

This module is domain-pure (no I/O, no clock reads beyond a `now` parameter
threaded in by the caller) — the application layer (calendar_store.py) owns
appending events and reading the wall clock.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from .events import (
    CalendarEventsImported,
    CalendarRefreshRejected,
    CalendarRefreshSucceeded,
    Event,
    EventWarningDismissed,
    ManualFireBlackoutAcknowledged,
    NoTradeTagRemoved,
    NoTradeTagSet,
    StandingCategoryRuleRemoved,
    StandingCategoryRuleSet,
)

# CAL-01: "Tier 1 -- official schedules ... Tier 2 -- Fed speakers". Any
# category outside this union is refused at the application boundary
# (calendar_store.py) — never silently accepted as a third, unspecced tier.
TIER_1: frozenset[str] = frozenset({"FOMC", "CPI", "NFP", "PPI", "PCE", "GDP"})
TIER_2: frozenset[str] = frozenset({"FED_SPEAKER"})
KNOWN_CATEGORIES: frozenset[str] = TIER_1 | TIER_2

# CAL-10 (v1.83): a THIRD event class -- computed by deterministic calendar
# math (application/opex_calendar.py), never fetched/imported. Deliberately
# NOT part of KNOWN_CATEGORIES: `import_events`/`record_refresh_success`
# (calendar_store.py) must keep raising UnknownCalendarCategory for these --
# "no fetch, no source domain" is a structural guarantee, not just prose.
COMPUTED_CATEGORIES: frozenset[str] = frozenset({"OPEX_MONTHLY", "QUAD_WITCH"})


def tier_for_category(category: str) -> int:
    """1 or 2 per CAL-01. Raises for anything outside `KNOWN_CATEGORIES` --
    never guessed."""
    if category in TIER_1:
        return 1
    if category in TIER_2:
        return 2
    raise ValueError(f"unknown calendar category: {category!r}")


@dataclass(frozen=True)
class CategoryImport:
    """The CURRENT import for one category (a later import for the same
    category REPLACES this, per `CalendarEventsImported`'s own docstring)."""

    category: str
    dates: frozenset[str] = frozenset()
    labels: dict[str, str] = field(default_factory=dict)   # date -> label override, "" excluded
    imported_at: str = ""
    source: str = ""
    # CAL-09 (v1.77): dates that WERE in a prior successful auto-refresh but
    # are absent from the MOST RECENT one -- still present in `dates` above
    # (never dropped) and still tag-effective (rule 2: "its NO-TRADE tag
    # stands until the operator rules"), just flagged for the UI/alerting.
    # A manual paste import (CalendarEventsImported) always resets this to
    # empty -- the operator's own replace is authoritative and clears any
    # outstanding dispute. Empty for every category an auto-refresh has
    # never touched.
    disputed: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TagInfo:
    day: str
    label: str
    origin: str                    # "manual" | "auto"
    category: str | None = None    # populated for origin == "auto"


@dataclass(frozen=True)
class CalendarState:
    imports: dict[str, CategoryImport] = field(default_factory=dict)     # category -> latest import
    standing_rules: dict[str, str | None] = field(default_factory=dict)  # category -> label override
    # day -> (label, origin). The origin is the EVENT's own `origin` field
    # passed through (never re-hardcoded here); in practice always "manual",
    # because auto-tags are DERIVED by `effective_tags` below from a live
    # rule + the current import — never journaled as tag events of their own.
    manual_tags: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Days whose AUTO-tag was individually suppressed (CAL-04) — persists
    # across a rule's removal/re-add and across a fresh import naming the
    # same date, by design ("removing one day does not resurrect
    # individually-removed days"). Only ever populated by a removal that
    # actually resolved to an effective auto-tag at fold time (see `apply`) —
    # never by removing a manual tag. Does NOT suppress a manual tag on the
    # same day (manual tags are a separate, always-effective layer applied
    # after auto-tags in `effective_tags`).
    removed_days: frozenset[str] = frozenset()
    # CAL-06 audit trail: day -> the label acknowledged, most recent wins.
    # Metadata only; report-tagging itself lives on CondorFilled.blackout_overridden.
    acknowledgments: dict[str, str] = field(default_factory=dict)
    # CAL-11: (category, event_date, proximity_tier) triples the operator has
    # dismissed -- each tier independent (dismissing T-3 never silences T-2/
    # T-1/T-0 for the same event). REC-07-durable by construction: the fold
    # rebuilds this set from `EventWarningDismissed` events alone.
    dismissed_warnings: frozenset[tuple[str, str, int]] = frozenset()


def apply(state: CalendarState, event: Event) -> CalendarState:
    """Pure single-event transition (mirrors domain/projection.py's `apply`).
    Unknown events pass through unchanged."""
    if isinstance(event, CalendarEventsImported):
        imports = dict(state.imports)
        labels = {d: lbl for d, lbl in zip(event.dates, event.labels) if lbl}
        imports[event.category] = CategoryImport(
            category=event.category, dates=frozenset(event.dates), labels=labels,
            imported_at=event.imported_at, source=event.source)
        return replace(state, imports=imports)
    if isinstance(event, CalendarRefreshSucceeded):
        # CAL-09: ADDITIVE merge, never a replace -- `event.dates` is already
        # the union the application layer (CalendarStore.record_refresh_success)
        # computed against the state at THAT time; this fold just installs it
        # verbatim (mirroring how CalendarEventsImported below installs its own
        # event fields verbatim -- neither event carries fold-time decisions).
        # `disputed` is fully REPLACED by this event's own set each time (not
        # unioned across refreshes): a date that reappears in a later fetch is
        # no longer disputed, and `event.dates`/`event.disputed_dates` were
        # already computed against the immediately-prior state, so this is the
        # single current truth, not a running accumulation.
        imports = dict(state.imports)
        labels = {d: lbl for d, lbl in zip(event.dates, event.labels) if lbl}
        imports[event.category] = CategoryImport(
            category=event.category, dates=frozenset(event.dates), labels=labels,
            imported_at=event.fetched_at, source=event.source,
            disputed=frozenset(event.disputed_dates))
        return replace(state, imports=imports)
    # CalendarRefreshRejected (CAL-09 rule 1) deliberately has NO case here:
    # "rejected whole" means existing data is untouched, and the fallthrough
    # at the bottom of this function already returns `state` unchanged for
    # any event it does not recognise -- the safest possible implementation
    # of reject-don't-replace is to not even attempt a transition.
    if isinstance(event, StandingCategoryRuleSet):
        rules = dict(state.standing_rules)
        rules[event.category] = event.label
        return replace(state, standing_rules=rules)
    if isinstance(event, StandingCategoryRuleRemoved):
        rules = dict(state.standing_rules)
        rules.pop(event.category, None)
        return replace(state, standing_rules=rules)
    if isinstance(event, NoTradeTagSet):
        tags = dict(state.manual_tags)
        # Finding 5 (2026-07-15 final review): the event's own origin passes
        # through — auto-tags are derived by `effective_tags`, never journaled.
        tags[event.day] = (event.label, event.origin)
        return replace(state, manual_tags=tags)
    if isinstance(event, NoTradeTagRemoved):
        # LAYERED REMOVAL (final-review finding 1, 2026-07-15). IMPLEMENTATION
        # DECISION, flagged for operator reversal (doc 11 is silent on the
        # manual/auto collision — same C-flag culture as its own C1..C8):
        #
        #   1. A removal pops the MANUAL layer first, and does NOT touch
        #      `removed_days` — regardless of whether the day is ALSO
        #      auto-tagged (a dual-layer day stays visibly auto-tagged; the
        #      operator removes again to suppress the auto layer too).
        #   2. Only a removal on a day with NO manual tag but an EFFECTIVE
        #      auto-tag suppresses the auto layer (CAL-04's per-day removal,
        #      persisting across rule re-add/re-import — unchanged).
        #   3. Neither layer present => harmless idempotent no-op.
        #
        # The pre-fix conflation ("pop manual AND suppress auto in one shot")
        # left a POISONED `removed_days` entry behind every manual tag-then-
        # untag: a LATER standing rule + import covering that day was
        # silently suppressed and the gate traded an FOMC day the operator
        # believed was covered (pinned fail-first in
        # tests/domain/test_trading_calendar_layers.py). Everything here is
        # derived from `state` at fold time — no new event fields — so old
        # journals replay deterministically under the new semantics.
        if event.day in state.manual_tags:
            tags = dict(state.manual_tags)
            tags.pop(event.day)
            return replace(state, manual_tags=tags)
        if _auto_tag_effective(state, event.day):
            return replace(state, removed_days=state.removed_days | {event.day})
        return state
    if isinstance(event, ManualFireBlackoutAcknowledged):
        acks = dict(state.acknowledgments)
        acks[event.day] = event.label
        return replace(state, acknowledgments=acks)
    if isinstance(event, EventWarningDismissed):
        dismissed = state.dismissed_warnings | {(event.category, event.event_date, event.tier)}
        return replace(state, dismissed_warnings=dismissed)
    return state


def _auto_tag_effective(state: CalendarState, day: str) -> bool:
    """True iff `day` currently resolves to an EFFECTIVE auto-tag: some live
    standing rule's category has a CURRENT import naming the day, and the day
    is not already individually suppressed. Pure state derivation — `apply`
    uses it so `NoTradeTagRemoved` only ever suppresses an auto-tag that
    actually exists at that point in the fold (layered removal, finding 1).

    CAL-10 (v1.83): a COMPUTED category (OPEX_MONTHLY/QUAD_WITCH) is NEVER
    imported, so `state.imports.get(category)` is always None for it -- this
    function cannot verify `day` is a REAL computed date without importing
    application/opex_calendar.py, which would violate the domain/application
    layering boundary (see that module's own docstring: "domain/ must never
    import application/"). The safe direction, taken here: a removal while
    ANY computed-category standing rule is live is treated as
    suppression-worthy regardless of which exact day was clicked.
    `effective_tags`/`label_for_day` only ever consult `removed_days` as an
    EXCLUSION FILTER over the caller-supplied real computed dates (see their
    own `computed_dates` handling below) -- a day that was never actually a
    computed event stays a harmless, inert entry here, while a day that WAS
    one (the operator's real "suppress" click) is now correctly and
    permanently suppressed. Without this, `NoTradeTagRemoved` on a
    computed-category auto-tag was a silent no-op: the tag kept reappearing
    on every read (pinned fail-first in
    tests/domain/test_trading_calendar_cal10_cal11.py)."""
    if day in state.removed_days:
        return False
    for category in state.standing_rules:
        imp = state.imports.get(category)
        if imp is not None and day in imp.dates:
            return True
        if category in COMPUTED_CATEGORIES:
            return True
    return False


def fold(events: list[Event]) -> CalendarState:
    """Rebuild calendar state from an ordered event log. Deterministic: equal
    input lists yield an equal CalendarState (the reboot-restore contract,
    TC-CAL-01 scenario 4)."""
    state = CalendarState()
    for event in events:
        state = apply(state, event)
    return state


def effective_tags(
    state: CalendarState, computed_dates: dict[str, frozenset[str]] | None = None
) -> dict[str, TagInfo]:
    """Every ET day currently tagged NO-TRADE, auto-tags first (so a manual
    tag on the same day — the operator's own direct act — always wins the
    label shown). Iteration order over `standing_rules`/`manual_tags` is
    insertion order (plain dicts); the two are not expected to collide in
    practice (CAL doesn't specify cross-category same-day precedence), and
    this module never silently drops either — the LAST write for a given day
    is what a plain dict naturally keeps, same as the fold above.

    `computed_dates` (CAL-10, v1.83): the SAME `{category: dates}` shape as
    `state.imports[category].dates`, but for a COMPUTED category
    (OPEX_MONTHLY/QUAD_WITCH) supplied by the caller (application/
    opex_calendar.py's `opex_dates_by_category`) rather than read from
    `state.imports` (computed categories are never imported, by design — see
    COMPUTED_CATEGORIES). A live standing rule against a computed category
    (e.g. "always block QUAD_WITCH") auto-tags exactly like a fetched
    category's rule does -- same `removed_days` suppression, same
    label-default-to-category-name logic, `origin="auto"`."""
    tags: dict[str, TagInfo] = {}
    for category, override_label in state.standing_rules.items():
        imp = state.imports.get(category)
        if imp is not None:
            for day in imp.dates:
                if day in state.removed_days:
                    continue
                label = override_label or imp.labels.get(day) or category
                tags[day] = TagInfo(day=day, label=label, origin="auto", category=category)
        computed = (computed_dates or {}).get(category)
        if computed:
            for day in computed:
                if day in state.removed_days:
                    continue
                label = override_label or category
                tags[day] = TagInfo(day=day, label=label, origin="auto", category=category)
    for day, (label, origin) in state.manual_tags.items():
        # `origin` is the journaled event's own field, passed through the fold
        # (finding 5) — always "manual" today: auto-tags are derived above,
        # never journaled as NoTradeTagSet events.
        tags[day] = TagInfo(day=day, label=label, origin=origin)
    return tags


def label_for_day(
    state: CalendarState, day: str, computed_dates: dict[str, frozenset[str]] | None = None
) -> str | None:
    """CAL-05's gate input: the NO-TRADE label in force for `day`, or None if
    untagged. CAL-07's fail-open polarity lives at the CALLER
    (application/calendar_store.py's `label_for_day` wrapper) — this pure
    function has no try/except of its own; a caller that cannot even fold
    the log is the one responsible for reading that as "no tag"."""
    tag = effective_tags(state, computed_dates).get(day)
    return tag.label if tag is not None else None


@dataclass(frozen=True)
class CategoryStaleness:
    imported_at: str
    horizon: str | None   # the latest imported date for this category, or None if empty
    stale: bool           # CAL-02: display-only — never blocks (CAL-07)


def staleness(state: CalendarState, *, now: datetime, stale_after_days: int) -> dict[str, CategoryStaleness]:
    """CAL-02: per-category `imported_at` + coverage horizon + a stale flag,
    for display only ("staleness is displayed, never hidden ... never
    blocks"). `now` must be tz-aware — the caller supplies the SAME clock
    convention every other freshness check in this codebase uses."""
    out: dict[str, CategoryStaleness] = {}
    for category, imp in state.imports.items():
        stale = False
        if imp.imported_at:
            try:
                imported_dt = datetime.fromisoformat(imp.imported_at)
                stale = (now - imported_dt) > timedelta(days=stale_after_days)
            except ValueError:
                stale = True   # unparsable stamp -- honestly unknown-fresh, treat as stale
        horizon = max(imp.dates) if imp.dates else None
        out[category] = CategoryStaleness(imported_at=imp.imported_at, horizon=horizon, stale=stale)
    return out


def consecutive_refresh_failures(events: list[Event], category: str) -> int:
    """CAL-09 rule 4: how many consecutive CALENDAR DAYS `category`'s daily
    auto-refresh has failed, most-recent-first -- the count application/
    calendar_refresh.py compares against `cal_refresh_fail_alert_days`
    before raising the persistent alert.

    Buckets `CalendarRefreshSucceeded`/`CalendarRefreshRejected` events for
    `category` by the calendar-day PREFIX of their own timestamp
    (`fetched_at`/`checked_at`, both `clock.now().isoformat()` -- same
    convention `staleness()` above already reads without ET conversion; a
    day-boundary edge case here is the same, already-accepted, honest
    simplification, not a new one). A day with a same-day retry keeps only
    that day's LAST outcome (a later dict write for the same key wins).

    A category with NO refresh events at all returns 0 -- CAL-07's polarity
    (absence never damns): a category that has simply never run yet (fresh
    install, or not due today) is not a "failing" one."""
    by_day: dict[str, bool] = {}
    for event in events:
        if isinstance(event, CalendarRefreshSucceeded) and event.category == category:
            by_day[event.fetched_at[:10]] = True
        elif isinstance(event, CalendarRefreshRejected) and event.category == category:
            by_day[event.checked_at[:10]] = False
    streak = 0
    for day in sorted(by_day, reverse=True):
        if by_day[day]:
            break
        streak += 1
    return streak


# --- CAL-11 event proximity warnings (v1.84) ---------------------------------

@dataclass(frozen=True)
class EventWarning:
    category: str
    event_date: str
    proximity_tier: int          # 0 = today, 1..N = trading days before
    label: str
    tier: int | None             # 1/2 per tier_for_category; None for a computed
                                  # category (OPEX_MONTHLY/QUAD_WITCH aren't tier-1/2)
    best_effort: bool             # tier == 2 (CAL-01: Fed speakers, never certain)
    computed: bool                # category in COMPUTED_CATEGORIES


def active_warnings(
    state: CalendarState,
    computed_dates: dict[str, frozenset[str]] | None,
    *,
    trading_days_path: list[str],
) -> list[EventWarning]:
    """CAL-11: trading_days_path[k] is the ET day k TRADING days from today
    (index 0 = today) -- precomputed by the CALLER via
    application/market_calendar.py's next_trading_day (this module never
    reads a clock or walks the exchange calendar itself -- same 'threaded in
    by the caller' discipline as staleness()'s `now` parameter). Emits one
    EventWarning per (category, event date) pair -- across EVERY imported
    tier-1/tier-2 category AND every computed category -- whose date matches
    an entry in trading_days_path, at that index's proximity tier, MINUS any
    (category, date, tier) already in state.dismissed_warnings (never
    re-nagged). Sorted nearest-first (ascending proximity_tier, CAL-11 rule
    4's stacking order) -- multiple events on the same or different days all
    appear independently, never fabricated (only emitted for a date the
    calendar's imports/computed set ACTUALLY carries)."""
    candidates: list[tuple[str, str, str]] = []   # (category, day, label)
    for category, imp in state.imports.items():
        for day in imp.dates:
            label = imp.labels.get(day) or category
            candidates.append((category, day, label))
    for category, dates in (computed_dates or {}).items():
        for day in dates:
            candidates.append((category, day, category))

    by_day = {day: k for k, day in enumerate(trading_days_path)}

    warnings: list[EventWarning] = []
    for category, day, label in candidates:
        k = by_day.get(day)
        if k is None:
            continue
        if (category, day, k) in state.dismissed_warnings:
            continue
        computed = category in COMPUTED_CATEGORIES
        tier = None if computed else tier_for_category(category)
        warnings.append(EventWarning(
            category=category, event_date=day, proximity_tier=k, label=label,
            tier=tier, best_effort=(tier == 2), computed=computed))

    warnings.sort(key=lambda w: (w.proximity_tier, w.event_date, w.category))
    return warnings
