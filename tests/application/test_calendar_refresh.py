"""CAL-09 (v1.77, doc 11) -- application/calendar_refresh.py unit coverage.

Drives `CalendarRefreshCoordinator` against fake `CalendarSource`s (never a
real network call) and a real `CalendarStore`/`CalendarState` fold, so the
merge/disputed/alerting/day-gate logic is proven against the ACTUAL domain
fold (domain/trading_calendar.py), not a mock of it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from meic.application.calendar_refresh import CalendarRefreshCoordinator, CategoryFetch
from meic.application.calendar_store import CalendarStore
from meic.domain.events import CalendarRefreshRejected, CalendarRefreshSucceeded

ET_LIKE = timezone.utc  # a fixed offset stands in for ET's own tz object in these unit tests


class _Clock:
    """A settable fake clock: `record_refresh_success`/`record_refresh_rejected`
    (application/calendar_store.py) stamp `fetched_at`/`checked_at` off THIS
    clock's `.now()`, not off whatever `now` a test happens to pass into
    `run_once` -- exactly like the real SystemClock and the real ET `now`
    `_probe_once` derives are two independent (if normally in-step) reads in
    production. Tests that simulate several distinct days must advance this
    clock alongside the `now` handed to `run_once`, or every event lands on
    the SAME calendar day regardless of which `now` was passed."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        self._now = now


class _FakeAlerts:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def alert(self, level: str, message: str, **context) -> None:
        self.calls.append((level, message, context))


class _FakeSource:
    """A CalendarSource stand-in whose `fetch()` returns a pre-scripted
    result list (or raises, if `raises` is set) -- one canned outcome per
    call, consumed off `_pending` (or repeated forever if only one is given)."""

    def __init__(self, categories: tuple[str, ...], results: list[list[CategoryFetch]] | None = None,
                 raises: Exception | None = None) -> None:
        self.categories = categories
        self._pending = list(results or [])
        self._raises = raises
        self.calls = 0

    async def fetch(self) -> list[CategoryFetch]:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        if len(self._pending) > 1:
            return self._pending.pop(0)
        return self._pending[0] if self._pending else []


def _coordinator(events, *, sources, fail_alert_days=3, now=None):
    clock = _Clock(now or datetime(2026, 7, 16, 9, 0, tzinfo=ET_LIKE))
    store = CalendarStore(events, clock)
    alerts = _FakeAlerts()
    coord = CalendarRefreshCoordinator(sources=tuple(sources), store=store, clock=clock,
                                       alerts=alerts, fail_alert_days=fail_alert_days)
    return coord, store, alerts


# --- should_run: day-gate + stale-boot catch-up ------------------------------

def test_should_run_true_on_a_fresh_install_with_no_attempt_ever():
    """The bootstrap case (review fix 1 reframed it): due immediately when
    NO refresh was ever ATTEMPTED -- not merely never succeeded, which
    would pin True forever against a structurally failing source."""
    events: list = []
    source = _FakeSource(("FOMC",))
    coord, _store, _alerts = _coordinator(events, sources=[source])
    now = datetime(2026, 7, 16, 9, 0, tzinfo=ET_LIKE)  # a Thursday, a real trading day
    assert coord.should_run(now) is True


def test_should_run_false_the_same_day_after_a_success_already_ran():
    events: list = []
    source = _FakeSource(("FOMC",), results=[[CategoryFetch(category="FOMC", ok=True,
                                                             dates=("2026-09-16",), url="u")]])
    coord, _store, _alerts = _coordinator(events, sources=[source])
    now = datetime(2026, 7, 16, 9, 0, tzinfo=ET_LIKE)
    asyncio.run(coord.run_once(now))
    later_same_day = now + timedelta(hours=1)
    assert coord.should_run(later_same_day) is False


def test_should_run_true_again_the_next_trading_day():
    events: list = []
    source = _FakeSource(("FOMC",), results=[[CategoryFetch(category="FOMC", ok=True,
                                                             dates=("2026-09-16",), url="u")]])
    coord, _store, _alerts = _coordinator(events, sources=[source])
    day1 = datetime(2026, 7, 16, 9, 0, tzinfo=ET_LIKE)
    asyncio.run(coord.run_once(day1))
    day2 = datetime(2026, 7, 17, 9, 0, tzinfo=ET_LIKE)  # the next trading day
    assert coord.should_run(day2) is True


def test_should_run_true_on_a_weekend_when_stale_boot_catchup_is_due():
    """CAL-09: "plus at boot if the last success is > 24 h old" overrides
    the ordinary trading-day gate -- a stale-boot catch-up must not wait
    for the next trading day if the operator's process was down a while."""
    events: list = [
        CalendarRefreshSucceeded(category="FOMC", dates=("2026-09-16",), labels=("",),
                                  added_dates=("2026-09-16",), disputed_dates=(),
                                  source="u", fetched_at="2026-07-10T09:00:00+00:00"),
    ]
    source = _FakeSource(("FOMC",))
    coord, _store, _alerts = _coordinator(events, sources=[source])
    a_saturday = datetime(2026, 7, 18, 9, 0, tzinfo=ET_LIKE)  # not a trading day
    assert coord.should_run(a_saturday) is True  # stale (> 24h) forces it anyway


def test_should_run_false_on_a_non_trading_day_when_fresh():
    events: list = [
        CalendarRefreshSucceeded(category="FOMC", dates=("2026-09-16",), labels=("",),
                                  added_dates=("2026-09-16",), disputed_dates=(),
                                  source="u", fetched_at="2026-07-17T09:00:00+00:00"),
    ]
    source = _FakeSource(("FOMC",))
    coord, _store, _alerts = _coordinator(events, sources=[source])
    a_saturday = datetime(2026, 7, 18, 9, 0, tzinfo=ET_LIKE)
    assert coord.should_run(a_saturday) is False


def test_should_run_false_after_a_same_day_rejected_run_the_severe_review_fix():
    """Review fix 1 (2026-07-16, SEVERE, reproduced): the pre-fix ordering
    put "some covered category has never SUCCEEDED => always due" BEFORE
    the attempted-today gate, so a structurally failing source (bls.gov's
    403 -- see bls.py) pinned `should_run` True forever: every ~60 s health
    tick re-fetched all sources all day and, once the fail-alert threshold
    hit, re-fired the CRITICAL every tick (flooding the 100-entry alert
    ring, RSK-06 exposure). The attempt gate now OUTRANKS every "due"
    reason: an attempt that ended in rejection is still today's attempt --
    the retry is tomorrow, not next tick."""
    events: list = []
    cats = ("CPI", "PPI", "NFP")
    bad = _FakeSource(cats, results=[[
        CategoryFetch(category=c, ok=False, reason="fetch_failed:403", url="u") for c in cats]])
    now = datetime(2026, 7, 16, 9, 0, tzinfo=ET_LIKE)
    coord, _store, _alerts = _coordinator(events, sources=[bad], now=now)

    assert coord.should_run(now) is True          # never attempted -- due
    asyncio.run(coord.run_once(now))
    # The rejected attempt still counts as today's attempt -- NOT re-due.
    assert coord.should_run(now + timedelta(minutes=1)) is False
    assert coord.should_run(now + timedelta(hours=7)) is False


def test_two_health_ticks_produce_one_attempt_not_two():
    """Review fix 1's tick-loop reproduction: driving the coordinator the
    way `_probe_once` does (should_run -> run_once) twice in one day must
    yield ONE attempt -- 3 rejection events for a 3-category source, never 6."""
    events: list = []
    now = datetime(2026, 7, 16, 9, 0, tzinfo=ET_LIKE)
    cats = ("CPI", "PPI", "NFP")
    bad = _FakeSource(cats, results=[[
        CategoryFetch(category=c, ok=False, reason="fetch_failed:403", url="u") for c in cats]])
    coord, _store, _alerts = _coordinator(events, sources=[bad], now=now)

    for tick in range(2):   # two health ticks, same day
        tick_now = now + timedelta(minutes=tick)
        if coord.should_run(tick_now):
            asyncio.run(coord.run_once(tick_now))
    rejected = [e for e in events if isinstance(e, CalendarRefreshRejected)]
    assert len(rejected) == 3, f"one attempt = 3 rejections (one per category), got {len(rejected)}"


def test_threshold_critical_fires_once_per_day_not_per_tick():
    """Review fix 1's alert-flood reproduction: once the consecutive-failure
    threshold is reached, later SAME-DAY ticks must not re-fire the
    critical -- driving the tick loop 5x per day for 3 days yields exactly
    ONE critical (day 3), not one per tick."""
    events: list = []
    day0 = datetime(2026, 7, 16, 9, 0, tzinfo=ET_LIKE)
    _coord0, store, alerts = _coordinator(events, sources=[], now=day0)

    for day_offset in range(3):
        day = day0 + timedelta(days=day_offset)
        store.clock.set(day)
        bad = _FakeSource(("FOMC",), results=[[CategoryFetch(
            category="FOMC", ok=False, reason="fetch_failed:403", url="u")]])
        day_coord = CalendarRefreshCoordinator(sources=(bad,), store=store, clock=store.clock,
                                               alerts=alerts, fail_alert_days=3)
        for tick in range(5):   # five health ticks per day
            tick_now = day + timedelta(minutes=tick)
            if day_coord.should_run(tick_now):
                asyncio.run(day_coord.run_once(tick_now))

    critical = [c for c in alerts.calls if c[0] == "critical"]
    assert len(critical) == 1, f"expected exactly one critical (day 3), got {len(critical)}"
    rejected = [e for e in events if isinstance(e, CalendarRefreshRejected)]
    assert len(rejected) == 3  # one attempt per day, three days


# --- run_once: success path ---------------------------------------------------

def test_run_once_records_a_success_and_no_alert_when_nothing_disputed():
    events: list = []
    source = _FakeSource(("FOMC",), results=[[CategoryFetch(category="FOMC", ok=True,
                                                             dates=("2026-09-16", "2026-10-28"),
                                                             url="https://www.federalreserve.gov/x")]])
    coord, store, alerts = _coordinator(events, sources=[source])
    asyncio.run(coord.run_once(datetime(2026, 7, 16, 9, 0, tzinfo=ET_LIKE)))

    succeeded = [e for e in events if isinstance(e, CalendarRefreshSucceeded)]
    assert len(succeeded) == 1
    assert succeeded[0].dates == ("2026-09-16", "2026-10-28")
    assert succeeded[0].added_dates == ("2026-09-16", "2026-10-28")
    assert succeeded[0].disputed_dates == ()
    assert store.state().imports["FOMC"].dates == {"2026-09-16", "2026-10-28"}
    # no disputed-date alert and no rejection alert; nothing crossed the
    # fail_alert_days streak either (this is the first ever attempt, and it
    # succeeded) -- so no alert at all fires for a clean run.
    assert alerts.calls == []


def test_run_once_is_additive_never_a_replace_and_marks_vanished_dates_disputed():
    """TC-CAL-03 scenario 3: "A vanished date is disputed, never dropped"."""
    events: list = []
    first = _FakeSource(("FOMC",), results=[[CategoryFetch(category="FOMC", ok=True,
                                                            dates=("2026-09-16", "2026-10-28"),
                                                            url="u")]])
    coord, store, alerts = _coordinator(events, sources=[first])
    asyncio.run(coord.run_once(datetime(2026, 7, 16, 9, 0, tzinfo=ET_LIKE)))

    # The operator ALSO tagged one of these days NO-TRADE manually.
    store.tag("2026-10-28", "FOMC")

    # A later fetch no longer sees 2026-10-28 (vanished) but adds a new date.
    second = _FakeSource(("FOMC",), results=[[CategoryFetch(category="FOMC", ok=True,
                                                             dates=("2026-09-16", "2026-12-09"),
                                                             url="u")]])
    coord2 = CalendarRefreshCoordinator(sources=(second,), store=store, clock=store.clock,
                                        alerts=alerts, fail_alert_days=3)
    asyncio.run(coord2.run_once(datetime(2026, 7, 17, 9, 0, tzinfo=ET_LIKE)))

    imp = store.state().imports["FOMC"]
    # ADDITIVE: the vanished date is STILL present (never dropped, rule 2).
    assert imp.dates == {"2026-09-16", "2026-10-28", "2026-12-09"}
    assert imp.disputed == {"2026-10-28"}
    # Its NO-TRADE tag STANDS untouched.
    assert store.label_for_day("2026-10-28") == "FOMC"
    # An alert fired for the disputed date.
    disputed_alerts = [c for c in alerts.calls if "DISPUTED" in c[1]]
    assert len(disputed_alerts) == 1
    assert disputed_alerts[0][0] == "warning"
    assert "2026-10-28" in disputed_alerts[0][2]["disputed_dates"]


def test_run_once_auto_tags_new_dates_under_a_standing_rule():
    """TC-CAL-03 scenario 1: "new events append with source and timestamp
    journaled" AND "a standing rule auto-tags the new dates"."""
    events: list = []
    source = _FakeSource(("FOMC",), results=[[CategoryFetch(category="FOMC", ok=True,
                                                             dates=("2026-09-16",),
                                                             url="https://www.federalreserve.gov/x")]])
    coord, store, _alerts = _coordinator(events, sources=[source])
    store.set_standing_rule("FOMC")  # "always block FOMC"

    asyncio.run(coord.run_once(datetime(2026, 7, 16, 9, 0, tzinfo=ET_LIKE)))

    assert store.label_for_day("2026-09-16") == "FOMC"
    succeeded = next(e for e in events if isinstance(e, CalendarRefreshSucceeded))
    assert succeeded.source == "https://www.federalreserve.gov/x"
    assert succeeded.fetched_at


# --- run_once: rejection path -------------------------------------------------

def test_run_once_rejects_whole_and_leaves_existing_data_byte_identical():
    """TC-CAL-03 scenario 2: "A garbage fetch can never damage existing
    data" -- fails / parses empty / returns an implausible count, rejected
    whole, existing events untouched, one alert."""
    events: list = []
    good = _FakeSource(("FOMC",), results=[[CategoryFetch(category="FOMC", ok=True,
                                                           dates=("2026-09-16",), url="u")]])
    coord, store, alerts = _coordinator(events, sources=[good])
    asyncio.run(coord.run_once(datetime(2026, 7, 16, 9, 0, tzinfo=ET_LIKE)))
    before = list(events)  # snapshot -- "byte-identical" after the bad fetch

    bad = _FakeSource(("FOMC",), results=[[CategoryFetch(category="FOMC", ok=False,
                                                          reason="implausible_count:40", url="u")]])
    coord2 = CalendarRefreshCoordinator(sources=(bad,), store=store, clock=store.clock,
                                        alerts=alerts, fail_alert_days=3)
    asyncio.run(coord2.run_once(datetime(2026, 7, 17, 9, 0, tzinfo=ET_LIKE)))

    assert store.state().imports["FOMC"].dates == {"2026-09-16"}  # untouched
    rejected = [e for e in events if isinstance(e, CalendarRefreshRejected)]
    assert len(rejected) == 1 and rejected[0].reason == "implausible_count:40"
    # existing SUCCESS events are byte-identical (nothing rewritten/replaced)
    assert [e for e in events if isinstance(e, CalendarRefreshSucceeded)] == \
           [e for e in before if isinstance(e, CalendarRefreshSucceeded)]
    reject_alerts = [c for c in alerts.calls if "REJECTED" in c[1]]
    assert len(reject_alerts) == 1 and reject_alerts[0][0] == "warning"


def test_run_once_never_crashes_when_a_source_itself_raises():
    events: list = []
    source = _FakeSource(("FOMC",), raises=RuntimeError("boom"))
    coord, store, alerts = _coordinator(events, sources=[source])
    asyncio.run(coord.run_once(datetime(2026, 7, 16, 9, 0, tzinfo=ET_LIKE)))  # must not raise

    rejected = [e for e in events if isinstance(e, CalendarRefreshRejected)]
    assert len(rejected) == 1 and "source_exception" in rejected[0].reason
    assert store.state().imports == {}


# --- run_once: fetch budgets (review fix 2, 2026-07-16) ----------------------

def test_a_hung_source_is_cancelled_at_the_per_source_ceiling(monkeypatch):
    """Review fix 2: `asyncio.wait_for` is the HARD ceiling above httpx's
    own per-request timeout -- a source that hangs past it is cancelled
    outright and recorded as a `fetch_timeout` rejection; the health tick
    is never held hostage."""
    import meic.application.calendar_refresh as cr

    monkeypatch.setattr(cr, "SOURCE_FETCH_TIMEOUT_S", 0.05)

    class _HungSource:
        categories = ("FOMC",)

        async def fetch(self):
            await asyncio.sleep(60)  # hangs far past any test budget

    events: list = []
    coord, _store, _alerts = _coordinator(events, sources=[_HungSource()])
    asyncio.run(coord.run_once(datetime(2026, 7, 16, 9, 0, tzinfo=ET_LIKE)))

    rejected = [e for e in events if isinstance(e, CalendarRefreshRejected)]
    assert len(rejected) == 1 and rejected[0].reason == "fetch_timeout"


def test_the_total_run_budget_skips_later_sources_as_rejected(monkeypatch):
    """Review fix 2: one slow source must not spend the WHOLE tick -- once
    the total budget is gone, later sources are recorded `budget_exhausted`
    (reject-don't-replace; retried on a later day) rather than fetched."""
    import meic.application.calendar_refresh as cr

    monkeypatch.setattr(cr, "RUN_TOTAL_BUDGET_S", 0.05)
    monkeypatch.setattr(cr, "SOURCE_FETCH_TIMEOUT_S", 30.0)

    class _SlowSource:
        categories = ("FOMC",)

        async def fetch(self):
            await asyncio.sleep(0.2)   # overruns the whole 0.05 s run budget
            return [CategoryFetch(category="FOMC", ok=True, dates=("2026-09-16",), url="u")]

    class _NeverReached:
        categories = ("GDP",)

        async def fetch(self):  # pragma: no cover -- the budget must prevent this call
            raise AssertionError("fetched past an exhausted budget")

    events: list = []
    coord, _store, _alerts = _coordinator(events, sources=[_SlowSource(), _NeverReached()])
    asyncio.run(coord.run_once(datetime(2026, 7, 16, 9, 0, tzinfo=ET_LIKE)))

    rejected = {e.category: e.reason for e in events if isinstance(e, CalendarRefreshRejected)}
    # the slow source was cut off at the remaining budget (fetch_timeout);
    # the source after it never ran at all (budget_exhausted).
    assert rejected["FOMC"] == "fetch_timeout"
    assert rejected["GDP"] == "budget_exhausted"


# --- record_refresh_success: store-level validation (review fix 4) -----------

def test_store_rejects_a_malformed_scraped_date_with_nothing_written():
    """Review fix 4: the STORE validates every date before appending
    anything -- no source adapter, present or future, can journal a
    malformed date (strict YYYY-MM-DD naming a real date, same bounds as
    app.py's `_cal_day`)."""
    import pytest

    from meic.application.calendar_store import InvalidCalendarRefreshData

    events: list = []
    _coord, store, _alerts = _coordinator(events, sources=[])
    for bad in ["2026-13-45", "garbage", "2026-07-16 ", "２０２６-07-16", "", None]:
        with pytest.raises(InvalidCalendarRefreshData):
            store.record_refresh_success(category="FOMC", dates=["2026-09-16", bad], source="u")
    assert events == []  # all-or-nothing: NOTHING was journaled


def test_store_rejects_an_unbounded_or_multiline_scraped_label():
    """Review fix 4: labels get the same bounds as app.py's `_cal_label`
    (<= 64 chars, printable single-line, strings only) -- rejected, never
    truncated, nothing written."""
    import pytest

    from meic.application.calendar_store import InvalidCalendarRefreshData

    events: list = []
    _coord, store, _alerts = _coordinator(events, sources=[])
    for bad in ["x" * 65, "two\nlines", "", 42]:
        with pytest.raises(InvalidCalendarRefreshData):
            store.record_refresh_success(category="FOMC", dates=["2026-09-16"],
                                          labels={"2026-09-16": bad}, source="u")
    assert events == []


def test_coordinator_turns_invalid_scraped_data_into_a_rejection():
    """Review fix 4 integration: a source that returns ok=True with garbage
    dates lands as a REJECTION (reason invalid_data:*) -- reject-don't-
    replace, existing data untouched, never a crash."""
    events: list = []
    good = _FakeSource(("FOMC",), results=[[CategoryFetch(
        category="FOMC", ok=True, dates=("2026-09-16",), url="u")]])
    coord, store, alerts = _coordinator(events, sources=[good])
    asyncio.run(coord.run_once(datetime(2026, 7, 16, 9, 0, tzinfo=ET_LIKE)))

    poisoned = _FakeSource(("FOMC",), results=[[CategoryFetch(
        category="FOMC", ok=True, dates=("2026-13-45", "not a date"), url="u")]])
    store.clock.set(datetime(2026, 7, 17, 9, 0, tzinfo=ET_LIKE))
    coord2 = CalendarRefreshCoordinator(sources=(poisoned,), store=store, clock=store.clock,
                                        alerts=alerts, fail_alert_days=3)
    asyncio.run(coord2.run_once(store.clock.now()))

    assert store.state().imports["FOMC"].dates == {"2026-09-16"}  # untouched
    rejected = [e for e in events if isinstance(e, CalendarRefreshRejected)]
    assert len(rejected) == 1 and rejected[0].reason.startswith("invalid_data:")


# --- run_once: consecutive-failure persistent alert --------------------------

def test_consecutive_failures_raise_a_persistent_critical_alert_after_the_threshold():
    """TC-CAL-03 scenario 4: "Feed failure is loud but never blocks
    trading" -- after `cal_refresh_fail_alert_days` consecutive failures a
    persistent alert raises; CAL-07 fail-open is untouched (label_for_day
    keeps returning None/normal answers throughout -- this coordinator
    never gates anything itself)."""
    events: list = []
    bad = _FakeSource(("FOMC",), results=[[CategoryFetch(category="FOMC", ok=False,
                                                          reason="fetch_failed:boom", url="u")]])
    day0 = datetime(2026, 7, 16, 9, 0, tzinfo=ET_LIKE)
    coord, store, alerts = _coordinator(events, sources=[bad], fail_alert_days=3, now=day0)

    asyncio.run(coord.run_once(day0))
    assert not any(c[0] == "critical" for c in alerts.calls)  # 1st failure: not yet

    store.clock.set(day0 + timedelta(days=1))  # advance the SAME clock the store stamps events with
    asyncio.run(coord.run_once(day0 + timedelta(days=1)))
    assert not any(c[0] == "critical" for c in alerts.calls)  # 2nd failure: not yet

    store.clock.set(day0 + timedelta(days=2))
    asyncio.run(coord.run_once(day0 + timedelta(days=2)))
    critical = [c for c in alerts.calls if c[0] == "critical"]
    assert len(critical) == 1  # 3rd consecutive failure: persistent alert fires
    assert "3 consecutive day" in critical[0][1]

    # Entries remain ungated by the calendar's absence throughout (CAL-07):
    # a category with no successful import ever still reads as untagged,
    # never as a block.
    assert store.label_for_day("2026-07-18") is None


def test_a_success_resets_the_failure_streak():
    events: list = []
    bad = _FakeSource(("FOMC",), results=[[CategoryFetch(category="FOMC", ok=False,
                                                          reason="fetch_failed:boom", url="u")]])
    day0 = datetime(2026, 7, 16, 9, 0, tzinfo=ET_LIKE)
    coord, store, alerts = _coordinator(events, sources=[bad], fail_alert_days=2, now=day0)
    asyncio.run(coord.run_once(day0))

    good = _FakeSource(("FOMC",), results=[[CategoryFetch(category="FOMC", ok=True,
                                                           dates=("2026-09-16",), url="u")]])
    store.clock.set(day0 + timedelta(days=1))
    coord2 = CalendarRefreshCoordinator(sources=(good,), store=store, clock=store.clock,
                                        alerts=alerts, fail_alert_days=2)
    asyncio.run(coord2.run_once(day0 + timedelta(days=1)))

    store.clock.set(day0 + timedelta(days=2))
    coord3 = CalendarRefreshCoordinator(sources=(bad,), store=store, clock=store.clock,
                                        alerts=alerts, fail_alert_days=2)
    asyncio.run(coord3.run_once(day0 + timedelta(days=2)))
    # Only ONE failure since the reset -- must not yet hit the 2-day threshold.
    assert not any(c[0] == "critical" for c in alerts.calls)
