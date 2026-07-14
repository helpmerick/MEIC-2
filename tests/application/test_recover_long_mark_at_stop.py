"""RPT-07 long recovery (2026-07-11, operator ruling): RecoverLong.recover()
stamps the long's mark-at-ladder-start onto LongSaleStarted -- the honest
mark-at-stop, whether reached via the push-detected path or a fallback
catch-up poll (RecoverLong itself doesn't distinguish the two; the stamp is
the same either way, taken from whatever Quote/intrinsic the caller has in
hand at THIS recover() call)."""
import asyncio
from datetime import datetime
from decimal import Decimal as D

from meic.application.recover_long import Quote, RecoverLong
from meic.domain.events import LongSaleStarted
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import ET, FastClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
SCHEDULED = datetime(2026, 7, 11, 12, 0, tzinfo=ET)


def _rec(events, broker=None, clock=None):
    # FastClock (not FakeClock): nothing drives a concurrent clock-advance
    # here, and RecoverLong's ladder does real `wait_until` reprice-gap
    # waits -- FastClock jumps straight to the deadline instead of blocking
    # (see tests/harness/fake_clock.py's own docstring on the two).
    return RecoverLong(broker or FakeBroker(), clock or FastClock(SCHEDULED), events, SPX)


def test_recover_stamps_mark_bid_ask_and_intrinsic_on_long_sale_started():
    events: list = []
    rec = _rec(events)

    asyncio.run(rec.recover(entry_id="e1", side="PUT", long_symbol="SPXW_5940P",
                            quote=Quote(bid=D("2.00"), ask=D("2.30")), intrinsic=D("0.75")))

    starts = [e for e in events if isinstance(e, LongSaleStarted)]
    assert len(starts) == 1
    s = starts[0]
    assert s.mark_bid == D("2.00")
    assert s.mark_ask == D("2.30")
    assert s.intrinsic == D("0.75")


def test_recover_stamps_the_quote_even_on_the_fallback_path():
    """LEX-02 invalid quote (crossed) -> straight to fallback -- the
    LongSaleStarted marker is still appended FIRST (recover()'s existing
    behaviour, unchanged), and it still carries the raw quote/intrinsic this
    call was given: the honest best-available mark, even though no ladder
    rung was ever priced off it."""
    events: list = []
    rec = _rec(events)

    asyncio.run(rec.recover(entry_id="e1", side="CALL", long_symbol="SPXW_5940C",
                            quote=Quote(bid=D("2.30"), ask=D("2.00")),  # crossed -> LEX-02 fallback
                            intrinsic=D("0")))

    starts = [e for e in events if isinstance(e, LongSaleStarted)]
    assert len(starts) == 1
    assert starts[0].mark_bid == D("2.30")
    assert starts[0].mark_ask == D("2.00")
    assert starts[0].intrinsic == D("0")


def test_old_style_long_sale_started_still_constructs_with_none_defaults():
    """Replay-safety at the dataclass level (mirrors StopPlaced.broker_order_id):
    a caller/older code path that only passes entry_id/side gets None for every
    new field, not a TypeError."""
    e = LongSaleStarted(entry_id="e1", side="PUT")
    assert e.mark_bid is None and e.mark_ask is None and e.intrinsic is None


# --- PNL-01: EC-LEX-08 floor path (rest_floor / record_floor_sold) ------------

def test_record_floor_sold_journals_a_non_zero_closing_fee():
    """EC-LEX-08: a resting intrinsic-floor sell discovered filled between
    ticks (`stop_fill_watch._try_recover` / `readopt_resting_floors`) calls
    this directly -- outside `recover()`'s own ladder -- so it needs its own
    fee wiring, not a free ride off `_sold`'s. Per-share: real $0.72
    (clearing+ORF+exchange, no commission on a close) / 100."""
    from meic.domain.events import LongSold

    events: list = []
    rec = _rec(events)
    rec.record_floor_sold("e1", "PUT", D("0.10"))
    sold = next(e for e in events if isinstance(e, LongSold))
    assert sold.fee == D("0.0072")  # closing a long: no commission


def test_record_floor_sold_fee_is_contracts_invariant():
    """PNL-01: the fee is PER-SHARE (see domain/fees.py) -- `entry_dollars`
    rescales by contracts exactly ONCE, at the reporting layer, off the
    entry's own leg qty. `record_floor_sold`'s own `qty` sizes the ORDER, not
    a second, double-counting fee multiplication."""
    from meic.domain.events import LongSold

    events: list = []
    rec = _rec(events)
    rec.record_floor_sold("e1", "PUT", D("0.10"), qty=2)
    sold = next(e for e in events if isinstance(e, LongSold))
    assert sold.fee == D("0.0072")
