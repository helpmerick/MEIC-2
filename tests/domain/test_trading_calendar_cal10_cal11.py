"""CAL-10/CAL-11 (doc 11, v1.83/v1.84) -- pure domain-fold coverage for
`domain/trading_calendar.py`'s computed-category and event-proximity-warning
extensions, isolated from `application/calendar_store.py`'s wiring (covered
separately in tests/application/test_calendar_store.py) and from the Gherkin
end-to-end binding (tests/bdd/test_tc_cal_04.py / test_tc_cal_05.py).

Mirrors tests/domain/test_trading_calendar_layers.py's own pure-fold style:
build a `CalendarState`/event list directly, call the module functions, and
assert on their return values -- no CalendarStore, no clock, no HTTP.
"""
from __future__ import annotations

from meic.domain.events import (
    EventWarningDismissed,
    StandingCategoryRuleSet,
)
from meic.domain.trading_calendar import (
    COMPUTED_CATEGORIES,
    CalendarState,
    EventWarning,
    active_warnings,
    apply,
    effective_tags,
    fold,
    label_for_day,
)


def _state_with_rule(category: str) -> CalendarState:
    return fold([StandingCategoryRuleSet(category=category, label=None)])


# --- CAL-10: computed categories are standing-rule capable, never fetched ----

class TestComputedCategoriesInEffectiveTags:
    def test_a_standing_rule_on_a_computed_category_auto_tags_its_computed_dates(self):
        state = _state_with_rule("QUAD_WITCH")
        computed = {"QUAD_WITCH": frozenset({"2026-09-18"}), "OPEX_MONTHLY": frozenset({"2026-07-17"})}
        tags = effective_tags(state, computed_dates=computed)
        assert tags["2026-09-18"].label == "QUAD_WITCH"
        assert tags["2026-09-18"].origin == "auto"
        assert tags["2026-09-18"].category == "QUAD_WITCH"
        # OPEX_MONTHLY has no standing rule in this state -- stays untagged.
        assert "2026-07-17" not in tags

    def test_label_for_day_reads_a_computed_auto_tag_exactly_like_a_fetched_one(self):
        state = _state_with_rule("QUAD_WITCH")
        computed = {"QUAD_WITCH": frozenset({"2026-09-18"})}
        assert label_for_day(state, "2026-09-18", computed_dates=computed) == "QUAD_WITCH"
        assert label_for_day(state, "2026-07-17", computed_dates=computed) is None

    def test_with_no_computed_dates_argument_nothing_breaks_or_auto_tags(self):
        """`computed_dates=None` (the default) must degrade to "no computed
        markers" -- never crash, never fabricate a tag (CAL-01's own honesty
        rule extended to the third event class)."""
        state = _state_with_rule("QUAD_WITCH")
        assert effective_tags(state) == {}
        assert label_for_day(state, "2026-09-18") is None

    def test_an_individually_removed_computed_day_stays_suppressed(self):
        """CAL-04's per-day removal semantics apply identically to a computed
        category's auto-tag (`removed_days` doesn't care where the date set
        came from)."""
        from meic.domain.events import NoTradeTagRemoved

        state = _state_with_rule("QUAD_WITCH")
        state = apply(state, NoTradeTagRemoved(day="2026-09-18"))
        computed = {"QUAD_WITCH": frozenset({"2026-09-18"})}
        assert label_for_day(state, "2026-09-18", computed_dates=computed) is None

    def test_computed_categories_are_disjoint_from_known_categories(self):
        from meic.domain.trading_calendar import KNOWN_CATEGORIES

        assert COMPUTED_CATEGORIES.isdisjoint(KNOWN_CATEGORIES)
        assert COMPUTED_CATEGORIES == frozenset({"OPEX_MONTHLY", "QUAD_WITCH"})


# --- CAL-11: active_warnings ---------------------------------------------------

class TestActiveWarnings:
    PATH = ["2026-07-15", "2026-07-16", "2026-07-17", "2026-07-20"]  # T0..T3, weekend-skipped

    def test_emits_one_warning_per_tier_for_a_fetched_category_across_the_path(self):
        from meic.domain.events import CalendarEventsImported

        state = fold([CalendarEventsImported(
            category="FOMC",
            dates=("2026-07-15", "2026-07-16", "2026-07-17", "2026-07-20"),
            labels=("", "", "", ""))])
        warnings = active_warnings(state, None, trading_days_path=self.PATH)
        by_tier = {w.proximity_tier: w for w in warnings}
        assert set(by_tier) == {0, 1, 2, 3}
        assert by_tier[0].event_date == "2026-07-15" and by_tier[0].label == "FOMC"
        assert by_tier[3].event_date == "2026-07-20"
        assert all(w.tier == 1 and w.best_effort is False and w.computed is False for w in warnings)

    def test_nearest_first_ordering(self):
        from meic.domain.events import CalendarEventsImported

        state = fold([CalendarEventsImported(
            category="FOMC", dates=("2026-07-20", "2026-07-15"), labels=("", ""))])
        warnings = active_warnings(state, None, trading_days_path=self.PATH)
        tiers = [w.proximity_tier for w in warnings]
        assert tiers == sorted(tiers)   # ascending == nearest-first (rule 4)

    def test_a_computed_category_emits_a_computed_warning_with_no_tier(self):
        computed = {"OPEX_MONTHLY": frozenset({"2026-07-17"}), "QUAD_WITCH": frozenset()}
        warnings = active_warnings(CalendarState(), computed, trading_days_path=self.PATH)
        assert len(warnings) == 1
        w = warnings[0]
        assert w.category == "OPEX_MONTHLY" and w.proximity_tier == 2
        assert w.tier is None and w.computed is True and w.best_effort is False

    def test_a_tier2_fed_speaker_event_is_marked_best_effort(self):
        from meic.domain.events import CalendarEventsImported

        state = fold([CalendarEventsImported(
            category="FED_SPEAKER", dates=("2026-07-16",), labels=("",))])
        warnings = active_warnings(state, None, trading_days_path=self.PATH)
        assert len(warnings) == 1
        assert warnings[0].tier == 2 and warnings[0].best_effort is True

    def test_never_fabricates_a_warning_for_a_date_off_the_calendar(self):
        """CAL-11 rule 3: only emitted for a date the imports/computed set
        ACTUALLY carries -- an empty state emits nothing, even across a full
        trading-days path."""
        assert active_warnings(CalendarState(), None, trading_days_path=self.PATH) == []

    def test_a_date_outside_the_trading_days_path_emits_no_warning(self):
        from meic.domain.events import CalendarEventsImported

        state = fold([CalendarEventsImported(category="FOMC", dates=("2026-08-01",), labels=("",))])
        assert active_warnings(state, None, trading_days_path=self.PATH) == []

    def test_dismissed_category_date_tier_triple_is_excluded_and_only_that_tier(self):
        from meic.domain.events import CalendarEventsImported

        state = fold([
            CalendarEventsImported(category="FOMC",
                                    dates=("2026-07-15", "2026-07-16", "2026-07-17", "2026-07-20"),
                                    labels=("", "", "", "")),
            EventWarningDismissed(category="FOMC", event_date="2026-07-20", tier=3),
        ])
        warnings = active_warnings(state, None, trading_days_path=self.PATH)
        by_tier = {w.proximity_tier for w in warnings}
        assert by_tier == {0, 1, 2}   # T-3 alone is gone

    def test_dismissing_one_tier_never_dismisses_another_tier_of_the_same_event(self):
        """Same (category, event_date) but a DIFFERENT date per tier here
        (CAL-11's real shape: one event date, several PROXIMITY tiers as the
        calendar counts down toward it) -- re-expressed at the fold level:
        dismissing (FOMC, 2026-07-20, 3) must not touch (FOMC, 2026-07-17, 2)."""
        state = fold([EventWarningDismissed(category="FOMC", event_date="2026-07-20", tier=3)])
        assert state.dismissed_warnings == {("FOMC", "2026-07-20", 3)}
        assert ("FOMC", "2026-07-17", 2) not in state.dismissed_warnings

    def test_a_second_identical_dismissal_is_idempotent_a_frozenset_stays_one_entry(self):
        state = fold([
            EventWarningDismissed(category="FOMC", event_date="2026-07-20", tier=3),
            EventWarningDismissed(category="FOMC", event_date="2026-07-20", tier=3),
        ])
        assert state.dismissed_warnings == {("FOMC", "2026-07-20", 3)}

    def test_replaying_the_same_event_log_twice_yields_an_equal_state_rec07(self):
        """REC-07: a dismissal is durable because the fold is deterministic --
        replaying the same log from scratch (the reboot-restore contract)
        reproduces an EQUAL CalendarState, never a re-nag."""
        events = [EventWarningDismissed(category="FOMC", event_date="2026-07-20", tier=3)]
        assert fold(events) == fold(list(events))


def test_event_warning_dataclass_shape():
    w = EventWarning(category="FOMC", event_date="2026-07-20", proximity_tier=3,
                      label="FOMC", tier=1, best_effort=False, computed=False)
    assert w.proximity_tier == 3 and w.tier == 1
