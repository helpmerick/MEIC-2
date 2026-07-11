"""LIVE-shaped DecayWatcher — the buyback/re-inflation races (DCY-02), preventatively.

`DecayWatcher` is not wired into the live/paper composition today (grep confirms
no `DecayWatcher(` outside this module and its own unit tests — flagged in
`stop_fill_watch.py`'s own docstring alongside the watchdog as a latent hazard).
These tests pin the 2026-07-11 sweep's guards now, so wiring it in later cannot
resurrect the race: `cancel.get("status") == "FILLED"` only matches
SimulatedBroker's shape — the LIVE TastytradeAdapter's cancel() never carries a
"status" key, so a genuine live race needs the fills-feed recheck this sweep adds.
"""
import asyncio
from datetime import datetime
from decimal import Decimal as D

from meic.application.decay_watcher import DecayWatcher
from tests.harness.fake_clock import ET, FakeClock
from tests.harness.live_broker import LiveShapedBroker

SCHEDULED = datetime(2026, 7, 11, 15, 50, tzinfo=ET)


def test_buyback_cancel_race_against_live_shape_is_still_caught():
    """The resting stop fills exactly as DCY-02's buyback tries to cancel it
    (`race_fill_on_cancel`). LiveShapedBroker's cancel() returns the ambiguous
    `{"result": "error", ...}` shape (no "status" key) a real broker would —
    the ORIGINAL `cancel.get("status") == "FILLED"` check can never see this;
    the fills-feed recheck must."""
    from meic.application.order_intent import protective_stop

    clock = FakeClock(SCHEDULED)
    broker = LiveShapedBroker(clock, fill_delay=10_000.0)
    events: list = []
    watcher = DecayWatcher(broker, events)

    stop_intent = protective_stop(entry_id="e1", right="P", contracts=1,
                                  trigger=D("0.05"), symbol="SPXW_7525P",
                                  idempotency_key="stop:e1:PUT")
    stop_id = asyncio.run(broker.submit(stop_intent))
    broker.race_fill_on_cancel(stop_id)  # fills DURING the buyback's cancel() call

    outcome = asyncio.run(watcher.buyback(
        entry_id="e1", side="PUT", resting_stop_id=stop_id, symbol="SPXW_7525P"))

    assert outcome == "STOP_FILLED_RUN_LEX"
    buy_submits = [s for s in broker.submits if s[1] == "limit"]
    assert not buy_submits, f"a buyback was submitted on an already-stopped-out leg: {buy_submits}"


def test_buyback_with_no_race_proceeds_normally():
    """Sanity check: the ordinary DCY-02 path (no race) is untouched."""
    from meic.application.order_intent import protective_stop

    clock = FakeClock(SCHEDULED)
    broker = LiveShapedBroker(clock, fill_delay=10_000.0)
    events: list = []
    watcher = DecayWatcher(broker, events)

    stop_intent = protective_stop(entry_id="e2", right="C", contracts=1,
                                  trigger=D("0.05"), symbol="SPXW_7550C",
                                  idempotency_key="stop:e2:CALL")
    stop_id = asyncio.run(broker.submit(stop_intent))

    outcome = asyncio.run(watcher.buyback(
        entry_id="e2", side="CALL", resting_stop_id=stop_id, symbol="SPXW_7550C"))

    assert outcome != "STOP_FILLED_RUN_LEX"
    buy_submits = [s for s in broker.submits if s[1] == "limit"]
    assert len(buy_submits) == 1


def test_reinflation_guard_never_reprotects_a_leg_the_buyback_already_closed():
    """The re-inflation guard's own cancel(buyback_id) races the buyback's own
    fill: if the buyback filled in that window, re-placing a stop would rest a
    phantom order on an already-flat leg. Must recognize the race and refuse
    to re-protect."""
    from meic.application.order_intent import buy_to_close_leg, OrderIntent

    clock = FakeClock(SCHEDULED)
    broker = LiveShapedBroker(clock, fill_delay=10_000.0)
    events: list = []
    watcher = DecayWatcher(broker, events)

    buyback_intent = OrderIntent(
        order_type="limit", tif="Day", kind="decay", entry_id="e3", contracts=1,
        price=D("0.05"), idempotency_key="decay:e3:PUT",
        legs=(buy_to_close_leg(right="P", contracts=1, symbol="SPXW_7525P"),))
    buyback_id = asyncio.run(broker.submit(buyback_intent))
    broker.fill_stop(buyback_id)  # the buyback itself filled

    outcome = asyncio.run(watcher.reinflation_guard(
        entry_id="e3", side="PUT", buyback_id=buyback_id, resting_stop_id="orig-stop",
        current_ask=D("0.10"), unfilled=True, symbol="SPXW_7525P", trigger=D("3.80")))

    assert outcome == "BUYBACK_ALREADY_FILLED"
    stop_submits = [s for s in broker.submits if s[1] == "stop_market"]
    assert not stop_submits, f"a phantom stop was rested on an already-closed leg: {stop_submits}"
