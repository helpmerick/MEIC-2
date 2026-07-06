"""Quote staleness — DAT-02."""
from datetime import datetime, timedelta
from decimal import Decimal as D

from meic.domain.staleness import StampedQuote

T0 = datetime(2026, 7, 6, 14, 0, 0)


def _q(bid, ask, stamped=T0):
    return StampedQuote("SPXW_5990P", D(bid), D(ask), stamped)


def test_fresh_quote_is_usable():
    q = _q("2.00", "2.10")
    now = T0 + timedelta(milliseconds=1000)
    assert not q.is_stale(now, max_age_ms=3000)
    assert q.usable(now, max_age_ms=3000)
    assert q.mid == D("2.05")


def test_stale_beyond_max_age():
    q = _q("2.00", "2.10")
    now = T0 + timedelta(milliseconds=3001)  # just past 3000ms
    assert q.is_stale(now, max_age_ms=3000)
    assert not q.usable(now, max_age_ms=3000)


def test_boundary_exactly_at_max_age_is_fresh():
    q = _q("2.00", "2.10")
    now = T0 + timedelta(milliseconds=3000)  # exactly at the age -> not > -> fresh
    assert not q.is_stale(now, max_age_ms=3000)


def test_crossed_quote_never_usable_even_if_fresh():
    q = _q("2.30", "2.10")  # bid > ask
    assert q.crossed
    assert not q.usable(T0, max_age_ms=3000)
