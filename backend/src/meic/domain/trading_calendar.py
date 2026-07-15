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
    Event,
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
    return state


def _auto_tag_effective(state: CalendarState, day: str) -> bool:
    """True iff `day` currently resolves to an EFFECTIVE auto-tag: some live
    standing rule's category has a CURRENT import naming the day, and the day
    is not already individually suppressed. Pure state derivation — `apply`
    uses it so `NoTradeTagRemoved` only ever suppresses an auto-tag that
    actually exists at that point in the fold (layered removal, finding 1)."""
    if day in state.removed_days:
        return False
    for category in state.standing_rules:
        imp = state.imports.get(category)
        if imp is not None and day in imp.dates:
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


def effective_tags(state: CalendarState) -> dict[str, TagInfo]:
    """Every ET day currently tagged NO-TRADE, auto-tags first (so a manual
    tag on the same day — the operator's own direct act — always wins the
    label shown). Iteration order over `standing_rules`/`manual_tags` is
    insertion order (plain dicts); the two are not expected to collide in
    practice (CAL doesn't specify cross-category same-day precedence), and
    this module never silently drops either — the LAST write for a given day
    is what a plain dict naturally keeps, same as the fold above."""
    tags: dict[str, TagInfo] = {}
    for category, override_label in state.standing_rules.items():
        imp = state.imports.get(category)
        if imp is None:
            continue
        for day in imp.dates:
            if day in state.removed_days:
                continue
            label = override_label or imp.labels.get(day) or category
            tags[day] = TagInfo(day=day, label=label, origin="auto", category=category)
    for day, (label, origin) in state.manual_tags.items():
        # `origin` is the journaled event's own field, passed through the fold
        # (finding 5) — always "manual" today: auto-tags are derived above,
        # never journaled as NoTradeTagSet events.
        tags[day] = TagInfo(day=day, label=label, origin=origin)
    return tags


def label_for_day(state: CalendarState, day: str) -> str | None:
    """CAL-05's gate input: the NO-TRADE label in force for `day`, or None if
    untagged. CAL-07's fail-open polarity lives at the CALLER
    (application/calendar_store.py's `label_for_day` wrapper) — this pure
    function has no try/except of its own; a caller that cannot even fold
    the log is the one responsible for reading that as "no tag"."""
    tag = effective_tags(state).get(day)
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
