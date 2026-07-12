"""LIVE-shaped EndOfDaySweep — a stop filling AT the closing-bell cancel race.

EOD-03 confirms zero working orders remain by re-reading `working_orders()`
after cancelling — but "no longer working" and "cleanly cancelled" are NOT the
same thing: an order can also be gone because it FILLED during the sweep's own
cancel call (a stop the market traded through right at the bell). Neither
broker's `cancel()` reliably reports that distinction (see `adapter.py`'s own
`replace()`/`_replace_fallback` docstrings), so the sweep must re-check the
fills feed itself before trusting "cancelled".

NOTE (2026-07-11 sweep): `EndOfDaySweep` is not wired into the live/paper
composition today (grep confirms no `EndOfDaySweep(` outside this module and
its own tests) — flagged separately for the operator. This guard is added
preventatively, exactly like the decay/watchdog guards, so wiring it in later
cannot resurrect this incident-#2-class race.
"""
import asyncio
from datetime import datetime
from decimal import Decimal as D

from meic.application.eod_sweep import EndOfDaySweep
from tests.harness.fake_clock import ET, FakeClock
from tests.harness.live_broker import LiveShapedBroker

SCHEDULED = datetime(2026, 7, 11, 16, 0, tzinfo=ET)


class _Alerts:
    def __init__(self):
        self.calls = []

    def alert(self, level, message, **ctx):
        self.calls.append((level, message, ctx))


def test_stop_filling_during_eod_cancel_is_flagged_not_reported_clean():
    """A resting stop fills exactly as the EOD sweep tries to cancel it
    (`race_fill_on_cancel`, the same hook `test_live_fill_path.py` uses for the
    entry-ladder version of this race). The order vanishes from
    working_orders() either way — the fix must tell "filled" apart from
    "cancelled" via the fills feed, not just absence from working_orders()."""
    from meic.application.order_intent import protective_stop

    clock = FakeClock(SCHEDULED)
    broker = LiveShapedBroker(clock, fill_delay=10_000.0)
    alerts = _Alerts()

    stop_intent = protective_stop(entry_id="e1", right="P", contracts=1,
                                  trigger=D("2.00"), symbol="SPXW_7525P",
                                  idempotency_key="stop:e1:PUT")
    stop_id = asyncio.run(broker.submit(stop_intent))
    broker.race_fill_on_cancel(stop_id)  # fills DURING the sweep's cancel() call

    sweep = EndOfDaySweep(broker, alerts)
    result = asyncio.run(sweep.sweep())

    assert result.raced_fills == [stop_id], result
    assert stop_id not in result.cancelled
    assert result.clean is True  # EOD-03's literal gate: zero WORKING orders remain
    assert any("FILLED while being cancelled" in msg for _, msg, _ in alerts.calls)


def test_clean_cancel_with_no_race_reports_cancelled_as_before():
    """Sanity check: the ordinary EOD case (no race) is untouched — the order
    is cleanly cancelled, no raced_fills, no extra alert."""
    from meic.application.order_intent import protective_stop

    clock = FakeClock(SCHEDULED)
    broker = LiveShapedBroker(clock, fill_delay=10_000.0)
    alerts = _Alerts()

    stop_intent = protective_stop(entry_id="e2", right="C", contracts=1,
                                  trigger=D("2.00"), symbol="SPXW_7550C",
                                  idempotency_key="stop:e2:CALL")
    stop_id = asyncio.run(broker.submit(stop_intent))

    sweep = EndOfDaySweep(broker, alerts)
    result = asyncio.run(sweep.sweep())

    assert result.cancelled == [stop_id]
    assert result.raced_fills == []
    assert result.clean is True
    assert alerts.calls == []
