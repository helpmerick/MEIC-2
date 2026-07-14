"""LIVE-shaped CloseEntry (CLS-01) — the replace-races-fill class, on the close path.

`close_entry.py`'s `_replace_stop` classifies `broker.replace()` per ORD-08 via
the typed `ReplaceFilled`/`ReplaceTerminal` exceptions. But the LIVE
TastytradeAdapter's own `replace()` docstring flags that it does NOT yet raise
`ReplaceFilled` for a genuine race — cert's cancel-failure payloads are
unverified, so every live replace failure (fill-race included) surfaces as a
bare exception, landing in ORD-08's "unclassifiable -> transient" bucket.
LiveShapedBroker mirrors that real, documented gap (its `replace()` raises a
plain RuntimeError on an already-filled target, never a typed ORD-08
exception) — the same shape `test_live_fill_path.py`/
`test_recover_long_live_fill_path.py` use for the entry/LEX ladders, now
driven through CLS's replace-based close.
"""
import asyncio
from decimal import Decimal as D

from meic.application.close_entry import CloseEntry, LiveLeg
from meic.application.order_intent import protective_stop
from meic.domain.events import ShortStopped
from tests.harness.fake_clock import ET, FakeClock
from tests.harness.live_broker import LiveShapedBroker

SCHEDULED_START = __import__("datetime").datetime(2026, 7, 11, 12, 0, tzinfo=ET)


class _Alerts:
    def __init__(self):
        self.calls = []

    def alert(self, level, message, **ctx):
        self.calls.append((level, message, ctx))


def test_replace_races_fill_is_detected_via_fills_since_not_left_resting():
    """The resting stop fills WHILE CloseEntry's replace() call races it.
    LiveShapedBroker raises the real adapter's plain RuntimeError (not
    ReplaceFilled) for an already-filled target, so `_replace_stop`'s retry
    loop exhausts as "unclassifiable". The post-retry fills_since recheck
    (this sweep's fix) must still recognize the fill: ShortStopped is
    journaled, and — critically — no second buy-to-close is ever submitted
    on the same leg."""
    clock = FakeClock(SCHEDULED_START)
    broker = LiveShapedBroker(clock, fill_delay=10_000.0)  # irrelevant to stops
    events: list = []
    alerts = _Alerts()
    close = CloseEntry(broker, events, alerts=alerts, replace_retry_attempts=2)

    stop_intent = protective_stop(entry_id="e1", right="P", contracts=1,
                                  trigger=D("2.00"), symbol="SPXW_7525P",
                                  idempotency_key="stop:e1:PUT")
    stop_id = asyncio.run(broker.submit(stop_intent))
    broker.fill_stop(stop_id)  # the resting stop fills — races the close

    live_leg = LiveLeg(symbol="SPXW_7525P", side="PUT", role="short", signed_qty=-1)
    asyncio.run(close.close("e1", "manual", resting_stop_ids={"PUT": stop_id},
                           live_legs=[live_leg], close_price=D("2.10")))

    stopped = [e for e in events if isinstance(e, ShortStopped)]
    assert len(stopped) == 1, "a replace-race fill must be recognized, not left ambiguous"
    assert stopped[0].side == "PUT"
    buy_submits = [s for s in broker.submits if s[1] == "marketable_limit"]
    assert not buy_submits, f"a second buy-to-close was submitted on an already-closed leg: {buy_submits}"
    # the misleading "left resting" alert must NOT fire once the race is
    # correctly recognized as a fill
    assert not any("left resting" in msg for _, msg, _ in alerts.calls)


def test_replace_succeeds_when_no_race_is_in_play():
    """Sanity check: with no race, the normal REPLACED path is untouched —
    CloseEntry still closes via one broker.replace() call, no extra fills_since
    scan changes that outcome."""
    clock = FakeClock(SCHEDULED_START)
    broker = LiveShapedBroker(clock, fill_delay=10_000.0)
    events: list = []
    close = CloseEntry(broker, events, alerts=_Alerts())

    stop_intent = protective_stop(entry_id="e2", right="C", contracts=1,
                                  trigger=D("2.00"), symbol="SPXW_7550C",
                                  idempotency_key="stop:e2:CALL")
    stop_id = asyncio.run(broker.submit(stop_intent))

    live_leg = LiveLeg(symbol="SPXW_7550C", side="CALL", role="short", signed_qty=-1)
    asyncio.run(close.close("e2", "manual", resting_stop_ids={"CALL": stop_id},
                           live_legs=[live_leg], close_price=D("2.10")))

    assert not [e for e in events if isinstance(e, ShortStopped)]
    buy_submits = [s for s in broker.submits if s[1] == "marketable_limit"]
    assert len(buy_submits) == 1, "the replace fallback should submit exactly one close"


def test_pnl01_replace_race_shortstopped_carries_a_non_zero_closing_fee():
    """PNL-01: the ShortStopped CloseEntry journals on an ORD-08a replace race
    (the SAME event `test_replace_races_fill_is_detected...` above proves) must
    carry a non-zero fee -- a closing buy-to-close, commission-free but not
    fee-free (clearing + ORF + exchange still apply)."""
    clock = FakeClock(SCHEDULED_START)
    broker = LiveShapedBroker(clock, fill_delay=10_000.0)
    events: list = []
    close = CloseEntry(broker, events, alerts=_Alerts(), replace_retry_attempts=2)

    stop_intent = protective_stop(entry_id="e3", right="P", contracts=1,
                                  trigger=D("2.00"), symbol="SPXW_7525P",
                                  idempotency_key="stop:e3:PUT")
    stop_id = asyncio.run(broker.submit(stop_intent))
    broker.fill_stop(stop_id)

    live_leg = LiveLeg(symbol="SPXW_7525P", side="PUT", role="short", signed_qty=-1)
    asyncio.run(close.close("e3", "manual", resting_stop_ids={"PUT": stop_id},
                           live_legs=[live_leg], close_price=D("2.10")))

    stopped = [e for e in events if isinstance(e, ShortStopped)]
    assert len(stopped) == 1
    # per-share: real $0.72 (clearing 0.10 + ORF 0.02 + SPX exchange 0.60, no
    # commission) / 100 -- matches `fill`'s own per-share scale.
    assert stopped[0].fee == D("0.0072")
