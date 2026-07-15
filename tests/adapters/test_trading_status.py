"""TradingStatusStore -- DAT-04a's halt-signal provider seam (v1.69,
operator-ratified, closes NFR-07's ninth finding). Pure logic, offline."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from meic.adapters.dxlink.trading_status import (
    ACTIVE_STATUS,
    HALT_READING_STALE_AFTER_SECONDS,
    TradingStatusReading,
    TradingStatusStore,
)

NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


def test_active_status_constant_matches_dxfeed_profile():
    # dxfeed Profile.trading_status's possible values are ACTIVE | HALTED |
    # UNDEFINED (tastytrade.dxfeed.profile.Profile) -- DAT-04a blocks on
    # anything that is not exactly this one tradeable value.
    assert ACTIVE_STATUS == "ACTIVE"


# --- unmeasured = unverified = blocked (RSK-07) -------------------------------

def test_no_reading_at_all_is_halted():
    store = TradingStatusStore()
    assert store.last is None
    assert store.halted(NOW) is True


# --- status polarity -----------------------------------------------------------

def test_active_and_fresh_is_not_halted():
    store = TradingStatusStore()
    store.record("ACTIVE", NOW)
    assert store.halted(NOW) is False


@pytest.mark.parametrize("status", ["HALTED", "UNDEFINED", "inactive", "garbage"])
def test_any_non_active_status_is_halted(status):
    store = TradingStatusStore()
    store.record(status, NOW)
    assert store.halted(NOW) is True


def test_record_is_case_and_whitespace_tolerant():
    store = TradingStatusStore()
    store.record(" active \n", NOW)
    assert store.last.status == "ACTIVE"
    assert store.halted(NOW) is False


# --- staleness (DAT-04a's 300s bound) -------------------------------------------

def test_reading_exactly_at_the_bound_is_not_stale():
    store = TradingStatusStore()
    store.record("ACTIVE", NOW - timedelta(seconds=HALT_READING_STALE_AFTER_SECONDS))
    assert store.halted(NOW) is False


def test_reading_one_second_beyond_the_bound_is_stale():
    store = TradingStatusStore()
    store.record("ACTIVE", NOW - timedelta(seconds=HALT_READING_STALE_AFTER_SECONDS + 1))
    assert store.halted(NOW) is True


def test_stale_reading_blocks_even_when_the_status_was_active():
    # Staleness is checked independently of status -- an old ACTIVE reading
    # is exactly the "the feed died while the market kept trading" case.
    store = TradingStatusStore()
    store.record("ACTIVE", NOW - timedelta(hours=1))
    assert store.halted(NOW) is True


# --- monotonic, never-regress discipline (mirrors QuoteHub.apply_tick) --------

def test_record_never_moves_the_reading_backward_in_time():
    store = TradingStatusStore()
    store.record("ACTIVE", NOW)
    store.record("HALTED", NOW - timedelta(seconds=5))   # an out-of-order, older tick
    assert store.last.status == "ACTIVE"   # the earlier tick is dropped, not applied
    assert store.halted(NOW) is False


def test_record_accepts_a_later_reading():
    store = TradingStatusStore()
    store.record("ACTIVE", NOW)
    store.record("HALTED", NOW + timedelta(seconds=1))
    assert store.last.status == "HALTED"
    assert store.halted(NOW + timedelta(seconds=1)) is True


def test_record_rejects_a_naive_instant():
    store = TradingStatusStore()
    with pytest.raises(ValueError, match="tz-aware"):
        store.record("ACTIVE", datetime(2026, 7, 15, 14, 0))   # DAY-03 discipline


def test_last_exposes_the_reading_dataclass():
    store = TradingStatusStore()
    store.record("ACTIVE", NOW)
    assert store.last == TradingStatusReading(status="ACTIVE", at=NOW)


# --- recovery: the gate must track BOTH directions -----------------------------

def test_halted_recovers_when_a_fresh_active_reading_arrives():
    store = TradingStatusStore()
    store.record("HALTED", NOW)
    assert store.halted(NOW) is True
    store.record("ACTIVE", NOW + timedelta(seconds=1))
    assert store.halted(NOW + timedelta(seconds=1)) is False
