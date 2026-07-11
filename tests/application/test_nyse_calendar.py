"""DAY-01/02: the NYSE holiday calendar and the next-trading-day walk
(operator ruling 2026-07-11 — the UI-24 countdown and the ENT-10 supervisor
must consult the exchange calendar; weekends/holidays are not trading days).

Every pinned date below is checkable against the exchange's published
calendars — the point of these tests is that the ALGORITHM reproduces the
published facts, so nobody ever maintains a list by hand.
"""
from datetime import date

from meic.application.market_calendar import is_trading_day, next_trading_day
from meic.application.nyse_holidays import (
    half_days_near,
    holidays_near,
    nyse_half_days,
    nyse_holidays,
)


class TestNyseHolidays:
    def test_day01_the_2026_calendar_is_exactly_the_published_one(self):
        assert nyse_holidays(2026) == frozenset({
            date(2026, 1, 1),    # New Year's Day (Thu)
            date(2026, 1, 19),   # MLK Day
            date(2026, 2, 16),   # Presidents' Day
            date(2026, 4, 3),    # Good Friday (Easter 2026-04-05)
            date(2026, 5, 25),   # Memorial Day
            date(2026, 6, 19),   # Juneteenth (Fri)
            date(2026, 7, 3),    # Independence Day observed (Jul 4 is a Saturday)
            date(2026, 9, 7),    # Labor Day
            date(2026, 11, 26),  # Thanksgiving
            date(2026, 12, 25),  # Christmas (Fri)
        })

    def test_day01_saturday_holidays_shift_to_friday(self):
        # July 4th 2026 is a Saturday -> observed Friday July 3rd.
        assert date(2026, 7, 3) in nyse_holidays(2026)
        assert date(2026, 7, 4) not in nyse_holidays(2026)
        # Christmas 2027 is a Saturday -> observed Friday December 24th.
        assert date(2027, 12, 24) in nyse_holidays(2027)

    def test_day01_sunday_holidays_shift_to_monday(self):
        # July 4th 2027 is a Sunday -> observed Monday July 5th.
        assert date(2027, 7, 5) in nyse_holidays(2027)
        # Christmas 2022 was a Sunday -> observed Monday December 26th.
        assert date(2022, 12, 26) in nyse_holidays(2022)

    def test_day01_new_years_on_saturday_is_not_observed_at_all(self):
        # Jan 1st 2028 is a Saturday: the NYSE does NOT move it back to
        # 2027-12-31 (the exchange was open on Friday 2021-12-31, the same
        # configuration). Neither year carries an observance.
        assert date(2027, 12, 31) not in nyse_holidays(2027)
        # ...and 2028 carries no New Year observance either (Jan 2nd 2028 is a
        # Sunday, so nothing shifts forward into the year).
        assert date(2028, 1, 1) not in nyse_holidays(2028)
        assert date(2028, 1, 3) not in nyse_holidays(2028)  # first Monday: open

    def test_day01_good_friday_tracks_easter(self):
        assert date(2025, 4, 18) in nyse_holidays(2025)   # Easter 2025-04-20
        assert date(2026, 4, 3) in nyse_holidays(2026)    # Easter 2026-04-05
        assert date(2027, 3, 26) in nyse_holidays(2027)   # Easter 2027-03-28
        assert date(2028, 4, 14) in nyse_holidays(2028)   # Easter 2028-04-16

    def test_day02_half_days_are_the_eves_and_black_friday(self):
        # 2025: July 3rd (Thu, July 4th Fri), day after Thanksgiving, Dec 24 (Wed).
        assert nyse_half_days(2025) == frozenset({
            date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)})
        # 2026: July 3rd is the OBSERVED July 4th holiday (full close), not a
        # half-day; Dec 24 is a Thursday half-day.
        assert nyse_half_days(2026) == frozenset({
            date(2026, 11, 27), date(2026, 12, 24)})

    def test_day01_holidays_near_spans_the_year_boundary(self):
        near = holidays_near(date(2026, 12, 28))
        assert date(2027, 1, 1) in near        # next year's New Year is visible
        assert date(2026, 12, 25) in near
        assert date(2027, 11, 25) in near      # a full year ahead
        half_near = half_days_near(date(2026, 12, 28))
        assert date(2027, 11, 26) in half_near


class TestNextTradingDay:
    HOLIDAYS_2026 = nyse_holidays(2026)

    def test_day01_a_saturday_rolls_to_monday(self):
        # The reported bug: Saturday 2026-07-11 promised an entry "today".
        assert next_trading_day(date(2026, 7, 11), holidays=self.HOLIDAYS_2026) \
            == date(2026, 7, 13)

    def test_day01_a_friday_evening_rolls_over_the_whole_weekend(self):
        assert next_trading_day(date(2026, 7, 10), holidays=self.HOLIDAYS_2026) \
            == date(2026, 7, 13)

    def test_day01_an_observed_holiday_extends_the_gap(self):
        # Thursday July 2nd 2026: Friday 3rd is the observed July 4th, then the
        # weekend -> Monday July 6th.
        assert next_trading_day(date(2026, 7, 2), holidays=self.HOLIDAYS_2026) \
            == date(2026, 7, 6)

    def test_day01_new_year_walk_needs_next_years_holidays(self):
        # Thursday 2026-12-31 -> Friday 2027-01-01 is a holiday -> Monday 01-04.
        assert next_trading_day(date(2026, 12, 31),
                                holidays=holidays_near(date(2026, 12, 31))) \
            == date(2027, 1, 4)

    def test_day01_is_trading_day_agrees_with_the_walk(self):
        assert not is_trading_day(date(2026, 7, 11), holidays=self.HOLIDAYS_2026)
        assert not is_trading_day(date(2026, 7, 3), holidays=self.HOLIDAYS_2026)
        assert is_trading_day(date(2026, 7, 13), holidays=self.HOLIDAYS_2026)
