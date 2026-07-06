"""DXLinkAdapter quote translation + staleness stamping — offline (DAT-02).

The live streaming path is contract-tested (pytest -m contract); the stamp
translation is pure and tested here without a socket.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal as D

from meic.adapters.dxlink.adapter import stamp_quote


@dataclass
class FakeDXQuote:
    event_symbol: str
    bid_price: float
    ask_price: float


T0 = datetime(2026, 7, 6, 14, 0, 0)


def test_stamp_quote_translates_and_stamps():
    q = stamp_quote(FakeDXQuote("SPX", 7532.02, 7535.86), now=T0)
    assert q.symbol == "SPX"
    assert q.bid == D("7532.02") and q.ask == D("7535.86")
    assert q.stamped_at == T0


def test_stamped_quote_goes_stale_by_the_clock():
    q = stamp_quote(FakeDXQuote("SPXW_5990P", 2.00, 2.10), now=T0)
    assert not q.is_stale(T0 + timedelta(milliseconds=2999), max_age_ms=3000)
    assert q.is_stale(T0 + timedelta(milliseconds=3001), max_age_ms=3000)


def test_stamp_tolerates_alt_field_names():
    @dataclass
    class AltQuote:
        symbol: str
        bid: float
        ask: float
    q = stamp_quote(AltQuote("SPX", 1.0, 1.2), now=T0)
    assert q.symbol == "SPX" and q.bid == D("1.0") and q.ask == D("1.2")
