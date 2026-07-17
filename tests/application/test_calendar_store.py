"""CalendarStore fail-open behaviour -- CAL-07, plus final-review finding 2
(2026-07-15): failing OPEN is ruled; failing SILENT is not.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from meic.application.calendar_store import CalendarStore
from tests.harness.fake_clock import FastClock

NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


class _BrokenEvents:
    """An event source whose iteration blows up -- the 'unreadable store'
    case CAL-07 rules must fail OPEN (trade), never closed."""

    def __iter__(self):
        raise RuntimeError("journal corrupted (synthetic)")


def test_cal07_unreadable_store_fails_open_and_logs(caplog):
    store = CalendarStore(_BrokenEvents(), FastClock(NOW))

    with caplog.at_level(logging.ERROR, logger="meic.application.calendar_store"):
        assert store.label_for_day("2026-07-15") is None   # CAL-07: fail-open

    # ... but NEVER silently (finding 2): exactly one traceback-carrying
    # record names the failure, so a fold bug can't disable every blackout
    # forever without a line in the log.
    records = [r for r in caplog.records if "CAL-07 fail-open" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is not None                 # full traceback attached
    assert "2026-07-15" in records[0].getMessage()


def test_cal07_a_healthy_empty_store_is_silent(caplog):
    """The fail-open LOG is for failures only -- an ordinary empty calendar
    (the everyday CAL-07 case) reads None with no log noise."""
    store = CalendarStore([], FastClock(NOW))
    with caplog.at_level(logging.ERROR, logger="meic.application.calendar_store"):
        assert store.label_for_day("2026-07-15") is None
    assert caplog.records == []


# --- CAL-10 (v1.83): computed OpEx/quad-witch events, taggable, never fetched --

def test_cal10_computed_events_exposes_the_pure_opex_math():
    store = CalendarStore([], FastClock(NOW))
    computed = store.computed_events(2026, 2026)
    assert computed["OPEX_MONTHLY"] and "2026-07-17" in computed["OPEX_MONTHLY"]
    assert "2026-09-18" in computed["QUAD_WITCH"]


def test_cal10_standing_rule_on_quad_witch_auto_tags_via_label_for_day_and_tags():
    store = CalendarStore([], FastClock(NOW))
    store.set_standing_rule("QUAD_WITCH")
    assert store.label_for_day("2026-09-18") == "QUAD_WITCH"     # auto-tagged
    assert store.label_for_day("2026-07-17") is None              # monthly OpEx, no rule for it
    tags = store.tags()
    assert tags["2026-09-18"].origin == "auto"
    assert "2026-07-17" not in tags


def test_cal10_import_events_still_rejects_computed_categories_never_fetched():
    from meic.application.calendar_store import UnknownCalendarCategory

    store = CalendarStore([], FastClock(NOW))
    for category in ("OPEX_MONTHLY", "QUAD_WITCH"):
        try:
            store.import_events(category=category, dates=["2026-07-17"])
            raise AssertionError(f"{category} must never be importable/fetched")
        except UnknownCalendarCategory:
            pass


def test_cal10_computed_categories_never_appear_in_staleness_report():
    """CAL-10: 'no staleness concept' -- a computed category, even with an
    active standing rule, must never show up in the CAL-02 staleness report
    (it was never imported, so it has no `imported_at`/horizon to report)."""
    store = CalendarStore([], FastClock(NOW))
    store.set_standing_rule("QUAD_WITCH")
    stale = store.staleness_report(stale_after_days=45)
    assert "QUAD_WITCH" not in stale and "OPEX_MONTHLY" not in stale


def test_cal10_removing_a_computed_auto_tag_actually_suppresses_it():
    """Regression for the fold-layer bug this feature surfaced (see
    tests/domain/test_trading_calendar_cal10_cal11.py): CAL-10 promises
    computed events are taggable "exactly like fetched ones" -- which
    includes the operator's per-day 'suppress auto-tag' removal, not just
    the initial tag."""
    store = CalendarStore([], FastClock(NOW))
    store.set_standing_rule("QUAD_WITCH")
    assert store.label_for_day("2026-09-18") == "QUAD_WITCH"
    store.untag("2026-09-18")
    assert store.label_for_day("2026-09-18") is None


def test_cal10_removing_one_computed_day_does_not_remove_the_standing_rule():
    store = CalendarStore([], FastClock(NOW))
    store.set_standing_rule("QUAD_WITCH")
    store.untag("2026-09-18")
    assert "QUAD_WITCH" in store.state().standing_rules   # rule itself untouched
    assert store.label_for_day("2027-03-19") == "QUAD_WITCH"  # a LATER quad-witch day still auto-tags


# --- CAL-11 (v1.84): event proximity warnings -----------------------------------

def test_cal11_active_warnings_spans_day_of_through_lead_days_weekend_skipped():
    """NOW = Wed 2026-07-15. T0..T3 (lead_days=3) walks Wed/Thu/Fri, skips
    the weekend, and lands T-3 on Monday 2026-07-20."""
    events, clock = [], FastClock(NOW)
    store = CalendarStore(events, clock)
    store.import_events(category="FOMC",
                         dates=["2026-07-15", "2026-07-16", "2026-07-17", "2026-07-20"])
    warnings = store.active_warnings(lead_days=3)
    fomc = {w.proximity_tier: w for w in warnings if w.category == "FOMC"}
    assert set(fomc) == {0, 1, 2, 3}
    assert fomc[0].event_date == "2026-07-15"
    assert fomc[3].event_date == "2026-07-20"   # weekend skipped, never Sat/Sun


def test_cal11_a_warning_never_gates_an_entry_untagged_day_still_trades():
    """CAL-11(1)/CAL-05: the warning feed and the blackout gate are
    independent reads -- an event the warning feed surfaces but the operator
    never tagged must still read as untagged for CAL-05's entry gate."""
    store = CalendarStore([], FastClock(NOW))
    store.import_events(category="FOMC", dates=["2026-07-17"])
    warnings = store.active_warnings(lead_days=3)
    assert any(w.event_date == "2026-07-17" for w in warnings)
    assert store.label_for_day("2026-07-17") is None   # CAL-05 gate: untagged, trades normally


def test_cal11_a_computed_opex_day_warns_but_its_untagged_day_still_trades():
    """TC-CAL-05 scenario 4's own vector: an untagged OpEx day with its
    warning showing still trades normally."""
    store = CalendarStore([], FastClock(NOW))   # NOW is 2026-07-15; 2026-07-17 is OPEX_MONTHLY
    warnings = store.active_warnings(lead_days=3)
    opex = [w for w in warnings if w.category == "OPEX_MONTHLY" and w.event_date == "2026-07-17"]
    assert len(opex) == 1 and opex[0].computed is True
    assert store.label_for_day("2026-07-17") is None   # never auto-blocked (no standing rule set)


def test_cal11_dismiss_one_tier_leaves_the_others_and_persists_across_a_rebuilt_store():
    """REC-07: dismissal is journaled append-only, so a FRESH CalendarStore
    over the SAME event list (the reboot-restore contract) must never re-nag
    the dismissed tier, while every other tier for the same event keeps
    showing -- 'the nearest warning is never pre-silenced by an earlier
    click' (CAL-11 rule 2)."""
    events, clock = [], FastClock(NOW)
    store = CalendarStore(events, clock)
    store.import_events(category="FOMC",
                         dates=["2026-07-15", "2026-07-16", "2026-07-17", "2026-07-20"])
    store.dismiss_warning("FOMC", "2026-07-20", 3)   # dismiss the T-3 banner

    warnings = store.active_warnings(lead_days=3)
    fomc_tiers = {w.proximity_tier for w in warnings if w.category == "FOMC"}
    assert fomc_tiers == {0, 1, 2}   # T-3 alone gone; day-of/T-1/T-2 still show

    # "Restart": rebuild a brand-new CalendarStore over the SAME journal.
    restarted = CalendarStore(list(events), FastClock(NOW))
    restarted_tiers = {w.proximity_tier for w in restarted.active_warnings(lead_days=3)
                       if w.category == "FOMC"}
    assert restarted_tiers == {0, 1, 2}   # never re-nags after "restart"


def test_cal11_dismiss_rejects_an_unknown_category():
    from meic.application.calendar_store import UnknownCalendarCategory

    store = CalendarStore([], FastClock(NOW))
    try:
        store.dismiss_warning("NOT_A_CATEGORY", "2026-07-15", 0)
        raise AssertionError("must reject an unknown category")
    except UnknownCalendarCategory:
        pass


def test_cal11_dismiss_accepts_a_computed_category():
    store = CalendarStore([], FastClock(NOW))
    store.dismiss_warning("OPEX_MONTHLY", "2026-07-17", 2)  # must not raise
    warnings = store.active_warnings(lead_days=3)
    assert not any(w.category == "OPEX_MONTHLY" and w.event_date == "2026-07-17" for w in warnings)


def test_cal11_tier2_fed_speaker_warning_is_marked_best_effort():
    store = CalendarStore([], FastClock(NOW))
    store.import_events(category="FED_SPEAKER", dates=["2026-07-16"])
    warnings = store.active_warnings(lead_days=3)
    speaker = [w for w in warnings if w.category == "FED_SPEAKER"]
    assert len(speaker) == 1 and speaker[0].best_effort is True and speaker[0].tier == 2


def test_cal11_never_fabricates_a_warning_for_an_uncalendared_date():
    store = CalendarStore([], FastClock(NOW))
    store.import_events(category="FOMC", dates=["2026-07-16"])
    warnings = store.active_warnings(lead_days=1)   # path only reaches 2026-07-16
    assert all(w.event_date in ("2026-07-15", "2026-07-16") for w in warnings)


def test_cal11_lead_days_zero_leaves_only_day_of():
    store = CalendarStore([], FastClock(NOW))
    store.import_events(category="FOMC", dates=["2026-07-15", "2026-07-16"])
    warnings = store.active_warnings(lead_days=0)
    fomc = [w for w in warnings if w.category == "FOMC"]
    assert len(fomc) == 1 and fomc[0].proximity_tier == 0 and fomc[0].event_date == "2026-07-15"
