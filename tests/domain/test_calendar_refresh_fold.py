"""CAL-09 (v1.77, doc 11) -- domain/trading_calendar.py's pure fold coverage
for the two new auto-refresh events (`CalendarRefreshSucceeded`/
`CalendarRefreshRejected`) and `consecutive_refresh_failures`. Application-
level coordinator behaviour (the merge/diff computation itself) is covered
by tests/application/test_calendar_refresh.py; this file only proves the
FOLD installs an already-computed event's fields correctly and that a
rejection never mutates state (rule 1's "rejected whole")."""
from __future__ import annotations

from meic.domain.events import (
    CalendarEventsImported,
    CalendarRefreshRejected,
    CalendarRefreshSucceeded,
    NoTradeTagSet,
)
from meic.domain.trading_calendar import consecutive_refresh_failures, fold


def test_refresh_succeeded_installs_dates_labels_and_disputed():
    events = [
        CalendarRefreshSucceeded(category="FOMC", dates=("2026-09-16", "2026-10-28"),
                                  labels=("", "FOMC decision"),
                                  added_dates=("2026-09-16", "2026-10-28"), disputed_dates=(),
                                  source="https://www.federalreserve.gov/x",
                                  fetched_at="2026-07-16T09:00:00+00:00"),
    ]
    state = fold(events)
    imp = state.imports["FOMC"]
    assert imp.dates == {"2026-09-16", "2026-10-28"}
    assert imp.labels == {"2026-10-28": "FOMC decision"}
    assert imp.source == "https://www.federalreserve.gov/x"
    assert imp.imported_at == "2026-07-16T09:00:00+00:00"
    assert imp.disputed == frozenset()


def test_refresh_rejected_never_mutates_state():
    """CAL-09 rule 1: "rejected whole" -- the fold's fallthrough leaves
    state untouched for an event it does not recognise; this pins that a
    rejection specifically never gets a case that could someday mutate it."""
    success = CalendarRefreshSucceeded(category="FOMC", dates=("2026-09-16",), labels=("",),
                                        added_dates=("2026-09-16",), disputed_dates=(),
                                        source="u", fetched_at="2026-07-16T09:00:00+00:00")
    before = fold([success])
    after = fold([success, CalendarRefreshRejected(category="FOMC", reason="parse_empty",
                                                    source="u", checked_at="2026-07-17T09:00:00+00:00")])
    assert after == before


def test_a_later_refresh_success_carries_forward_disputed_and_clears_reappeared_dates():
    events = [
        CalendarRefreshSucceeded(category="FOMC", dates=("2026-09-16", "2026-10-28"), labels=("", ""),
                                  added_dates=("2026-09-16", "2026-10-28"), disputed_dates=(),
                                  source="u", fetched_at="2026-07-16T09:00:00+00:00"),
        # Next day: 2026-10-28 vanished (disputed), a new date appears.
        CalendarRefreshSucceeded(category="FOMC", dates=("2026-09-16", "2026-10-28", "2026-12-09"),
                                  labels=("", "", ""), added_dates=("2026-12-09",),
                                  disputed_dates=("2026-10-28",),
                                  source="u", fetched_at="2026-07-17T09:00:00+00:00"),
    ]
    state = fold(events)
    imp = state.imports["FOMC"]
    assert imp.dates == {"2026-09-16", "2026-10-28", "2026-12-09"}  # never dropped
    assert imp.disputed == {"2026-10-28"}
    # 2026-10-28 REAPPEARS on the next fetch -- no longer disputed.
    events.append(CalendarRefreshSucceeded(
        category="FOMC", dates=("2026-09-16", "2026-10-28", "2026-12-09"), labels=("", "", ""),
        added_dates=(), disputed_dates=(), source="u", fetched_at="2026-07-18T09:00:00+00:00"))
    state2 = fold(events)
    assert state2.imports["FOMC"].disputed == frozenset()


def test_a_manual_paste_import_resets_disputed_to_empty():
    """CAL-09: the operator's own replace (CalendarEventsImported) is
    authoritative and clears any outstanding dispute -- consistent with it
    already replacing `dates`/`labels`/`source` wholesale."""
    events = [
        CalendarRefreshSucceeded(category="FOMC", dates=("2026-09-16", "2026-10-28"), labels=("", ""),
                                  added_dates=("2026-09-16", "2026-10-28"),
                                  disputed_dates=("2026-10-28",),
                                  source="u", fetched_at="2026-07-16T09:00:00+00:00"),
        CalendarEventsImported(category="FOMC", dates=("2026-09-16",), labels=("",),
                                imported_at="2026-07-16T10:00:00+00:00", source="pasted_table"),
    ]
    state = fold(events)
    assert state.imports["FOMC"].disputed == frozenset()


def test_a_disputed_day_still_auto_tags_under_a_standing_rule():
    """CAL-09 rule 2: "its NO-TRADE tag stands until the operator rules" --
    a disputed date is STILL in `imports[category].dates`, so a standing
    rule keeps auto-tagging it exactly as before."""
    from meic.domain.events import StandingCategoryRuleSet
    from meic.domain.trading_calendar import label_for_day

    events = [
        StandingCategoryRuleSet(category="FOMC", label=None),
        CalendarRefreshSucceeded(category="FOMC", dates=("2026-10-28",), labels=("",),
                                  added_dates=("2026-10-28",), disputed_dates=("2026-10-28",),
                                  source="u", fetched_at="2026-07-17T09:00:00+00:00"),
    ]
    state = fold(events)
    assert label_for_day(state, "2026-10-28") == "FOMC"


def test_a_disputed_day_with_a_manual_tag_still_stands():
    from meic.domain.trading_calendar import label_for_day

    events = [
        CalendarRefreshSucceeded(category="FOMC", dates=("2026-09-16", "2026-10-28"), labels=("", ""),
                                  added_dates=("2026-09-16", "2026-10-28"), disputed_dates=(),
                                  source="u", fetched_at="2026-07-16T09:00:00+00:00"),
        NoTradeTagSet(day="2026-10-28", label="FOMC", origin="manual"),
        CalendarRefreshSucceeded(category="FOMC", dates=("2026-09-16", "2026-10-28"), labels=("", ""),
                                  added_dates=(), disputed_dates=("2026-10-28",),
                                  source="u", fetched_at="2026-07-17T09:00:00+00:00"),
    ]
    state = fold(events)
    assert label_for_day(state, "2026-10-28") == "FOMC"


# --- consecutive_refresh_failures ---------------------------------------------

def test_consecutive_refresh_failures_zero_when_never_attempted():
    assert consecutive_refresh_failures([], "FOMC") == 0


def test_consecutive_refresh_failures_counts_trailing_rejections():
    events = [
        CalendarRefreshSucceeded(category="FOMC", dates=("2026-09-16",), labels=("",),
                                  added_dates=("2026-09-16",), disputed_dates=(),
                                  source="u", fetched_at="2026-07-14T09:00:00+00:00"),
        CalendarRefreshRejected(category="FOMC", reason="fetch_failed:x", source="u",
                                 checked_at="2026-07-15T09:00:00+00:00"),
        CalendarRefreshRejected(category="FOMC", reason="fetch_failed:x", source="u",
                                 checked_at="2026-07-16T09:00:00+00:00"),
    ]
    assert consecutive_refresh_failures(events, "FOMC") == 2


def test_consecutive_refresh_failures_resets_on_a_success():
    events = [
        CalendarRefreshRejected(category="FOMC", reason="x", source="u",
                                 checked_at="2026-07-14T09:00:00+00:00"),
        CalendarRefreshRejected(category="FOMC", reason="x", source="u",
                                 checked_at="2026-07-15T09:00:00+00:00"),
        CalendarRefreshSucceeded(category="FOMC", dates=("2026-09-16",), labels=("",),
                                  added_dates=("2026-09-16",), disputed_dates=(),
                                  source="u", fetched_at="2026-07-16T09:00:00+00:00"),
    ]
    assert consecutive_refresh_failures(events, "FOMC") == 0


def test_consecutive_refresh_failures_is_scoped_to_its_own_category():
    events = [
        CalendarRefreshRejected(category="FOMC", reason="x", source="u",
                                 checked_at="2026-07-16T09:00:00+00:00"),
        CalendarRefreshSucceeded(category="CPI", dates=("2026-07-16",), labels=("",),
                                  added_dates=("2026-07-16",), disputed_dates=(),
                                  source="u", fetched_at="2026-07-16T09:00:00+00:00"),
    ]
    assert consecutive_refresh_failures(events, "FOMC") == 1
    assert consecutive_refresh_failures(events, "CPI") == 0


def test_consecutive_refresh_failures_a_same_day_retry_keeps_only_the_last_outcome():
    events = [
        CalendarRefreshRejected(category="FOMC", reason="x", source="u",
                                 checked_at="2026-07-16T09:00:00+00:00"),
        CalendarRefreshSucceeded(category="FOMC", dates=("2026-09-16",), labels=("",),
                                  added_dates=("2026-09-16",), disputed_dates=(),
                                  source="u", fetched_at="2026-07-16T12:00:00+00:00"),
    ]
    assert consecutive_refresh_failures(events, "FOMC") == 0
