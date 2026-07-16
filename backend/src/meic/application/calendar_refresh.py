"""CalendarRefreshCoordinator -- CAL-09 (v1.77) daily official-source
auto-refresh application service (doc 11).

Drives one or more `CalendarSource` adapters (adapters/calendar_sources/*),
each a read-only, unauthenticated fetch against exactly ONE of the three
named domains (federalreserve.gov/bls.gov/bea.gov), through ONE daily pass:

  * a source/category that parses cleanly and passes its plausibility band
    is recorded via `CalendarStore.record_refresh_success` -- ADDITIVE
    (union with whatever the category already had), never a replace
    (rule 1/2). A previously-known date absent from this fetch is marked
    DISPUTED (never dropped) and alerted; its NO-TRADE tag stands untouched
    (CAL-04/05 read the SAME `imports[category].dates`, which still
    contains it).
  * anything else -- a network/HTTP failure, an empty parse, an implausible
    count, or a wrong-host refusal the adapter itself raised -- is recorded
    via `record_refresh_rejected` ONLY: no mutating event is ever appended
    for a rejected category, so existing data is untouched by construction
    (rule 1's "rejected whole").

Every outcome is alerted (rule 3's "everything evented"); CAL-07's fail-open
polarity is never touched here -- a broken feed changes nothing about what
ENT-06/CalendarStore.label_for_day reads, and this coordinator's own
`run_once` never raises past itself (a source that raises is caught and
recorded as a rejection for every category it declares, never a crashed
loop -- the daily health tick that drives this must survive it exactly like
the EOD-03 sweep / RPT-15 reconcile survive their own failures).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from meic.application.calendar_store import (
    CalendarStore,
    InvalidCalendarRefreshData,
    UnknownCalendarCategory,
)
from meic.application.market_calendar import is_trading_day, trading_day
from meic.application.nyse_holidays import holidays_near
from meic.domain.trading_calendar import KNOWN_CATEGORIES, consecutive_refresh_failures

logger = logging.getLogger(__name__)

# Review fix 2 (2026-07-16): fetch budgets. The health tick this refresh
# rides also carries DAT-02 snapshot freshness, the EOD-03 sweep and the
# RPT-15 reconcile -- CAL-09 must never hold it for minutes. httpx's own
# per-request `timeout` (15 s, adapters/calendar_sources/common.py) bounds
# each network primitive; these are the HARD ceilings above it:
# `asyncio.wait_for` per source (an adapter that misbehaves past its own
# httpx timeouts is cancelled outright) and a total per-run budget across
# all sources (a source the budget cannot reach is recorded rejected --
# `budget_exhausted` -- and retried on a later day like any other failure).
# Internal safety constants in the SUBSCRIBE_SPAN_PTS style -- doc 06
# defines no operator dial for them.
SOURCE_FETCH_TIMEOUT_S = 30.0
RUN_TOTAL_BUDGET_S = 90.0


@dataclass(frozen=True)
class CategoryFetch:
    """One category's outcome from one source's `fetch()` call. `ok=False`
    covers every REJECT-WHOLE cause CAL-09 rule 1 names (network/HTTP
    failure, wrong host, empty parse, implausible count) -- the adapter
    tells them apart in `reason`; this coordinator only needs to know
    whether to record a success or a rejection."""
    category: str
    ok: bool
    dates: tuple[str, ...] = ()
    labels: dict[str, str] | None = None
    reason: str = ""
    url: str = ""


class CalendarSource(Protocol):
    """One official source (adapters/calendar_sources/*): a read-only,
    unauthenticated fetch against ONE named domain, covering one or more
    tier-1 categories. `fetch()` must never raise past itself -- any
    failure (network, parse, plausibility, wrong host) is reported as a
    `CategoryFetch(ok=False, ...)` for every category the source declares,
    never an exception the coordinator has to guess how to handle."""

    categories: tuple[str, ...]

    async def fetch(self) -> list[CategoryFetch]: ...


@dataclass
class CalendarRefreshCoordinator:
    sources: tuple[CalendarSource, ...]
    store: CalendarStore
    clock: object
    alerts: object
    fail_alert_days: int = 3
    _covered: frozenset[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        covered: set[str] = set()
        for source in self.sources:
            covered.update(source.categories)
        # CAL-01: a source declaring a category outside the known tier-1/
        # tier-2 union is a wiring bug -- caught here, once, at construction,
        # rather than surfacing as a confusing per-fetch UnknownCalendarCategory.
        unknown = covered - KNOWN_CATEGORIES
        if unknown:
            raise UnknownCalendarCategory(sorted(unknown)[0])
        self._covered = frozenset(covered)

    def should_run(self, now: datetime) -> bool:
        """CAL-09: at most ONE attempt per calendar day, ever -- the
        once-per-attempted-day gate OUTRANKS every "due" reason below it
        (review fix 1, 2026-07-16, SEVERE, reproduced: the pre-fix ordering
        put a "some covered category has never SUCCEEDED => always due"
        fast path FIRST, so with bls.gov structurally 403'd -- see bls.py --
        `should_run` pinned True forever: all three sources re-fetched every
        ~60 s health tick all day, and once the fail-alert threshold hit,
        the CRITICAL re-fired every tick, flooding the 100-entry alert
        ring (RSK-06 exposure). An attempt that ENDED IN REJECTION is still
        an attempt -- the retry is tomorrow, not next tick).

        Below the attempt gate, in order: a covered category with no
        refresh attempt on record at all (fresh install) is due
        immediately; a stale last-success (> 24 h, or a covered category
        that has attempts but no success yet) is due even on a non-trading
        day (CAL-09's "at boot if the last success is > 24 h old" catch-up
        -- checked every tick, which is simpler and strictly more forgiving
        than a literal boot-only check since `_probe_once` runs once at
        boot and then every tick); otherwise once per trading day.

        `now` must be tz-aware ET (the caller's job, mirroring every other
        day-gated tick in adapters/api/server.py, e.g. `_maybe_eod_sweep_once`)."""
        today_str = now.date().isoformat()
        attempted_ever = False
        for event in self.store.events:
            if getattr(event, "category", None) not in self._covered:
                continue
            stamp = getattr(event, "fetched_at", None) or getattr(event, "checked_at", None)
            if not stamp:
                continue
            attempted_ever = True
            try:
                if trading_day(datetime.fromisoformat(stamp)).isoformat() == today_str:
                    return False  # already attempted today (success OR rejection) -- never re-run
            except ValueError:
                continue  # an unparsable stamp cannot prove "attempted today"
        if not attempted_ever:
            return True  # fresh install: nothing ever attempted -- due now (once; the
            #              attempt itself journals events that flip the gate above)
        imports = self.store.state().imports
        successes = [imports[c].imported_at for c in self._covered
                     if c in imports and imports[c].imported_at]
        if len(successes) < len(self._covered):
            # some covered category has attempts but no success yet (e.g.
            # bls.gov 403) -- stale by definition, due on ANY day, but still
            # at most once per day (the attempt gate above already returned
            # False if today's attempt happened).
            return True
        try:
            oldest = min(datetime.fromisoformat(s) for s in successes)
            stale_boot_catchup = (now - oldest) > timedelta(hours=24)
        except ValueError:
            stale_boot_catchup = True  # unparsable stamp -- honestly unknown-fresh, treat as due
        if stale_boot_catchup:
            return True
        today = now.date()
        return is_trading_day(today, holidays=holidays_near(today))

    async def run_once(self, now: datetime) -> None:
        # Review fix 2 (2026-07-16): a hard wall-clock budget for the WHOLE
        # pass, plus a per-source `asyncio.wait_for` ceiling -- see the
        # module constants' comment. A source the remaining budget cannot
        # reach records `budget_exhausted` rejections for its categories
        # (reject-don't-replace; retried on a later day like any failure).
        deadline = time.monotonic() + RUN_TOTAL_BUDGET_S
        for source in self.sources:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("CAL-09: run budget exhausted before source %r", source)
                results = [CategoryFetch(category=c, ok=False, reason="budget_exhausted")
                           for c in source.categories]
            else:
                try:
                    results = await asyncio.wait_for(
                        source.fetch(), timeout=min(SOURCE_FETCH_TIMEOUT_S, remaining))
                except asyncio.TimeoutError:
                    logger.warning("CAL-09: source %r exceeded its fetch budget", source)
                    results = [CategoryFetch(category=c, ok=False, reason="fetch_timeout")
                               for c in source.categories]
                except Exception as exc:  # noqa: BLE001 -- a source must NEVER crash this loop
                    logger.warning("CAL-09: source %r raised during fetch: %r", source, exc)
                    results = [CategoryFetch(category=c, ok=False,
                                             reason=f"source_exception:{exc!r}")
                               for c in source.categories]
            for result in results:
                self._record(result)

    def _record(self, result: CategoryFetch) -> None:
        try:
            if result.ok and result.dates:
                try:
                    ev = self.store.record_refresh_success(
                        category=result.category, dates=list(result.dates),
                        labels=result.labels, source=result.url)
                except InvalidCalendarRefreshData as bad:
                    # Review fix 4 (2026-07-16): the store's own validation
                    # gate refused a date/label a source scraped -- treated
                    # as one more reject-whole cause (rule 1), never a crash
                    # and never a partial write (the store validates BEFORE
                    # appending anything).
                    self._record(CategoryFetch(category=result.category, ok=False,
                                               reason=f"invalid_data:{bad.reason}",
                                               url=result.url))
                    return
                if ev.disputed_dates:
                    self.alerts.alert(
                        "warning",
                        f"CAL-09: {result.category} -- {len(ev.disputed_dates)} previously-"
                        f"imported date(s) absent from today's official fetch, marked DISPUTED; "
                        f"any NO-TRADE tag on them stands until the operator rules: "
                        f"{', '.join(ev.disputed_dates)}",
                        category=result.category, disputed_dates=list(ev.disputed_dates))
            else:
                reason = result.reason or "parse_empty"
                self.store.record_refresh_rejected(
                    category=result.category, reason=reason, source=result.url)
                self.alerts.alert(
                    "warning",
                    f"CAL-09: {result.category} auto-refresh REJECTED ({reason}) -- existing "
                    f"calendar data is unchanged (reject-don't-replace)",
                    category=result.category, reason=reason)
        except UnknownCalendarCategory:
            logger.error("CAL-09: source declared unknown category %r", result.category)
            return
        streak = consecutive_refresh_failures(self.store.events, result.category)
        if streak >= self.fail_alert_days:
            self.alerts.alert(
                "critical",
                f"CAL-09: {result.category} auto-refresh has failed {streak} consecutive day(s) "
                f"(>= cal_refresh_fail_alert_days={self.fail_alert_days}) -- calendar entries "
                f"remain UNGATED by this absence (CAL-07 fail-open); manual paste import remains "
                f"available",
                category=result.category, consecutive_failures=streak)
