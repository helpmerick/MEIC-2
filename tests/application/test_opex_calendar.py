"""CAL-10 (doc 11, v1.83): computed OpEx/quad-witch events
(application/opex_calendar.py) -- deterministic calendar math, no fetch, no
I/O. Every date below is independently checkable against the exchange's
published third-Friday OpEx calendar and the DAY-01 NYSE holiday calendar.
"""
from datetime import date

from meic.application.market_calendar import is_trading_day
from meic.application.nyse_holidays import nyse_holidays
from meic.application.opex_calendar import (
    QUAD_WITCH_MONTHS,
    _third_friday,
    opex_dates_by_category,
    opex_events_for_year,
    opex_events_for_years,
)


class TestThirdFriday:
    def test_matches_known_third_fridays(self):
        assert _third_friday(2026, 7) == date(2026, 7, 17)
        assert _third_friday(2026, 9) == date(2026, 9, 18)
        assert _third_friday(2000, 4) == date(2000, 4, 21)

    def test_is_always_a_friday_in_the_right_week(self):
        for year in (2024, 2025, 2026, 2027):
            for month in range(1, 13):
                d = _third_friday(year, month)
                assert d.weekday() == 4  # Friday
                assert 15 <= d.day <= 21  # the third Friday always falls here


class TestOpexEventsForYear:
    def test_quad_witch_months_are_exactly_march_june_september_december(self):
        assert QUAD_WITCH_MONTHS == frozenset({3, 6, 9, 12})

    def test_2026_monthly_and_quarterly_opex_compute_correctly(self):
        events = opex_events_for_year(2026)
        # TC-CAL-04 scenario 1's own vectors.
        assert events["2026-07-17"] == "OPEX_MONTHLY"   # third Friday of July 2026
        assert events["2026-09-18"] == "QUAD_WITCH"     # third Friday of Sept 2026 (quad-witch month)

    def test_exactly_twelve_entries_no_weekly_or_daily_expiration_renders(self):
        for year in (2024, 2025, 2026, 2027, 2030):
            events = opex_events_for_year(year)
            assert len(events) == 12   # one per month, never more
            assert len(set(events.values()) - {"OPEX_MONTHLY", "QUAD_WITCH"}) == 0

    def test_quad_witch_months_get_quad_witch_never_a_second_stacked_entry(self):
        events = opex_events_for_year(2026)
        by_month = {int(day.split("-")[1]): cat for day, cat in events.items()}
        for month in range(1, 13):
            expected = "QUAD_WITCH" if month in QUAD_WITCH_MONTHS else "OPEX_MONTHLY"
            assert by_month[month] == expected
        # exactly one computed event per month -- never a second, separate
        # OPEX_MONTHLY entry stacked on the same day as a QUAD_WITCH one.
        assert len(events) == 12

    def test_good_friday_2000_shifts_to_the_preceding_trading_day(self):
        """TC-CAL-04 scenario 2's real vector: April 2000, Good Friday the 21st."""
        holidays_2000 = nyse_holidays(2000)
        assert date(2000, 4, 21) in holidays_2000                 # Good Friday
        assert _third_friday(2000, 4) == date(2000, 4, 21)         # AND the 3rd Friday
        assert not is_trading_day(date(2000, 4, 21), holidays=holidays_2000)

        events = opex_events_for_year(2000)
        assert "2000-04-20" in events    # preceding trading day (a Thursday)
        assert events["2000-04-20"] == "OPEX_MONTHLY"
        assert "2000-04-21" not in events   # never lands on the holiday itself

    def test_additional_holiday_shift_vectors(self):
        """More real vectors beyond April 2000, found by scanning the
        algorithm itself against the published NYSE holiday calendar (each
        independently checkable): Good Friday falling on the third Friday of
        April/March in several years, and Juneteenth (June 19) falling on the
        third Friday of June (a quad-witch month) in 2026."""
        # 2025-04-18 is Good Friday AND the third Friday of April 2025.
        holidays_2025 = nyse_holidays(2025)
        assert date(2025, 4, 18) in holidays_2025
        events_2025 = opex_events_for_year(2025)
        assert "2025-04-17" in events_2025 and events_2025["2025-04-17"] == "OPEX_MONTHLY"
        assert "2025-04-18" not in events_2025

        # 2008-03-21 is Good Friday AND the third Friday of March 2008 (a
        # QUAD_WITCH month).
        holidays_2008 = nyse_holidays(2008)
        assert date(2008, 3, 21) in holidays_2008
        events_2008 = opex_events_for_year(2008)
        assert "2008-03-20" in events_2008 and events_2008["2008-03-20"] == "QUAD_WITCH"
        assert "2008-03-21" not in events_2008

        # 2026-06-19 is Juneteenth AND the third Friday of June 2026 (a
        # QUAD_WITCH month) -- shifts to 2026-06-18.
        holidays_2026 = nyse_holidays(2026)
        assert date(2026, 6, 19) in holidays_2026
        events_2026 = opex_events_for_year(2026)
        assert "2026-06-18" in events_2026 and events_2026["2026-06-18"] == "QUAD_WITCH"
        assert "2026-06-19" not in events_2026

    def test_no_duplicate_dates_across_the_year(self):
        for year in (2000, 2008, 2025, 2026, 2027):
            events = opex_events_for_year(year)
            assert len(events) == len(set(events.keys()))


class TestOpexEventsForYears:
    def test_unions_multiple_years(self):
        combined = opex_events_for_years([2025, 2026])
        assert "2025-04-17" in combined
        assert "2026-07-17" in combined
        assert len(combined) == 24


class TestOpexDatesByCategory:
    def test_shape_is_per_category_frozensets(self):
        by_cat = opex_dates_by_category([2026])
        assert set(by_cat.keys()) == {"OPEX_MONTHLY", "QUAD_WITCH"}
        assert isinstance(by_cat["OPEX_MONTHLY"], frozenset)
        assert isinstance(by_cat["QUAD_WITCH"], frozenset)
        assert "2026-07-17" in by_cat["OPEX_MONTHLY"]
        assert "2026-09-18" in by_cat["QUAD_WITCH"]
        assert "2026-07-17" not in by_cat["QUAD_WITCH"]
        assert len(by_cat["OPEX_MONTHLY"]) + len(by_cat["QUAD_WITCH"]) == 12

    def test_spans_multiple_years(self):
        by_cat = opex_dates_by_category(range(2025, 2028))
        assert len(by_cat["OPEX_MONTHLY"]) + len(by_cat["QUAD_WITCH"]) == 36
