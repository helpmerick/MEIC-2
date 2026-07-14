"""DAY-03: `trading_day`/`trading_day_str` -- the ONE shared "what ET trading
day is it" helper (application/market_calendar.py).

THE BUG THIS FIXES (found live, 2026-07-13, reproduced on the running panel):
`adapters/api/server.py`'s `commands.day()` used to derive "today" via
`datetime.now(timezone.utc).astimezone().date().isoformat()`. `.astimezone()`
with NO argument converts to the SYSTEM's local timezone -- whatever the OS/
operator's machine happens to be set to -- never ET. A BST operator's local
midnight (7pm ET, harmless after the close) or a Tokyo operator's local
midnight (11am ET, MID-SESSION) would silently stamp the wrong trading day
onto every entry id, and the real cert trade `2026-07-13#2` vanished from the
operator's own `/entries` board this way.

Every case below constructs a tz-AWARE UTC instant and asserts the ET
calendar date directly -- no dependency on the test runner's own OS-local
timezone (DAY-03 conversion goes through the IANA `America/New_York` zoneinfo
rules, which are identical on every machine), so these pass and mean the same
thing regardless of where they run.
"""
from datetime import datetime, timezone

import pytest

from meic.application.market_calendar import trading_day, trading_day_str


def test_late_evening_utc_is_still_the_same_et_trading_day():
    """THE confirmed live bug, pinned: 23:53 UTC on 2026-07-13 is 19:53 EDT
    the SAME day -- a BST operator's `.astimezone()`-with-no-arg bug would
    have rolled this to 2026-07-14 (BST local midnight is 23:00 UTC in
    summer); the ET trading day is unambiguously still 2026-07-13."""
    now = datetime(2026, 7, 13, 23, 53, tzinfo=timezone.utc)
    assert trading_day_str(now) == "2026-07-13"
    assert trading_day(now).isoformat() == "2026-07-13"


def test_mid_session_utc_date_already_rolled_over_still_resolves_the_et_day():
    """The Tokyo case: 15:00 UTC on 2026-07-13 is 11:00 EDT -- mid-session --
    while Asia/Tokyo (UTC+9) local wall-clock date is ALREADY 2026-07-14. An
    entry fired at this instant must be stamped `2026-07-13#n`, never
    tomorrow's date, regardless of the operator's machine timezone."""
    now = datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)
    assert trading_day_str(now) == "2026-07-13"


def test_shortly_after_utc_midnight_is_still_the_prior_et_trading_day():
    """00:30 UTC on 2026-07-14 is 20:30 EDT on the 13th -- the UTC calendar
    date has already rolled to the 14th, but ET (and the trading day) has
    not."""
    now = datetime(2026, 7, 14, 0, 30, tzinfo=timezone.utc)
    assert trading_day_str(now) == "2026-07-13"


def test_utc_morning_is_the_new_et_trading_day():
    """13:00 UTC on 2026-07-14 is 09:00 EDT -- both UTC and ET have rolled to
    the 14th by this point (ET's own midnight is 04:00 UTC in summer)."""
    now = datetime(2026, 7, 14, 13, 0, tzinfo=timezone.utc)
    assert trading_day_str(now) == "2026-07-14"


@pytest.mark.parametrize(
    "utc_instant, expected_et_day",
    [
        # EST (UTC-5, January -- no DST): 20:30 UTC = 15:30 EST, same day.
        (datetime(2026, 1, 15, 20, 30, tzinfo=timezone.utc), "2026-01-15"),
        # Just past EST local midnight (05:00 UTC = 00:00 EST) is the NEW day.
        (datetime(2026, 1, 15, 5, 0, tzinfo=timezone.utc), "2026-01-15"),
        (datetime(2026, 1, 15, 4, 59, tzinfo=timezone.utc), "2026-01-14"),
        # EDT (UTC-4, July -- DST in effect): 23:53 UTC = 19:53 EDT, same day.
        (datetime(2026, 7, 13, 23, 53, tzinfo=timezone.utc), "2026-07-13"),
        # Just past EDT local midnight (04:00 UTC = 00:00 EDT) is the NEW day.
        (datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc), "2026-07-14"),
        (datetime(2026, 7, 14, 3, 59, tzinfo=timezone.utc), "2026-07-13"),
    ],
)
def test_dst_correct_across_est_and_edt(utc_instant, expected_et_day):
    """The whole point of `ZoneInfo("America/New_York")` over a fixed offset:
    the ET boundary sits at a different UTC hour in EST (UTC-5, January) than
    in EDT (UTC-4, July) -- a hardcoded offset would get one of the two
    wrong."""
    assert trading_day_str(utc_instant) == expected_et_day


def test_naive_datetime_is_refused_not_silently_guessed():
    """A caller with a naive datetime has an unstated timezone -- exactly the
    ambiguity that produced the live bug. This must be loud, never a silent
    guess at UTC or the OS local zone."""
    with pytest.raises(ValueError):
        trading_day(datetime(2026, 7, 13, 23, 53))
