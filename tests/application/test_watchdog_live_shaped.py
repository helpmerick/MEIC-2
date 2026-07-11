"""LIVE-shaped StopWatchdog — the escalation double-fill race (STP-03b), preventatively.

`StopWatchdog` is not wired into the live/paper composition today (grep confirms
no `StopWatchdog(` outside this module and its own unit tests — flagged in
`stop_fill_watch.py`'s own docstring as a latent hazard: "journals ShortStopped
BEFORE its marketable buy-back confirms"). `escalate()` already re-checks the
resting stop is not filled immediately before submitting its own marketable
buy-back (the existing TC-STP-17 scenario 3 pins that), but the window between
THAT check and the submit() call itself remained open — a fill landing there
means BOTH the resting stop and the escalation's own buy both executed, and the
broker cannot un-submit the second one. This pins the 2026-07-11 sweep's guard:
recognize the double-fill and alert loudly rather than silently cancelling (a
no-op — the stop is already gone) and journaling a clean single escalation.
"""
import asyncio
from datetime import datetime
from decimal import Decimal as D

from meic.application.watchdog import StopWatchdog
from meic.domain.events import ShortStopped
from tests.harness.fake_clock import ET, FakeClock
from tests.harness.live_broker import LiveShapedBroker

SCHEDULED = datetime(2026, 7, 11, 11, 0, tzinfo=ET)


class _Alerts:
    def __init__(self):
        self.calls = []

    def alert(self, level, message, **ctx):
        self.calls.append((level, message, ctx))


def test_resting_stop_fills_between_precheck_and_submit_is_alerted_not_silent():
    """The resting stop fills exactly as escalate()'s own marketable buy-back is
    being submitted — after the pre-check passed, so the submit genuinely goes
    through. Both orders are now real fills; the fix must recognize this and
    alert, never silently cancel (a no-op) and journal a clean single
    escalation as if only one buy ever happened."""
    from meic.application.order_intent import protective_stop

    clock = FakeClock(SCHEDULED)
    broker = LiveShapedBroker(clock, fill_delay=10_000.0)
    events: list = []
    alerts = _Alerts()
    wd = StopWatchdog(broker=broker, alerts=alerts, events=events)

    stop_intent = protective_stop(entry_id="e1", right="P", contracts=1,
                                  trigger=D("3.80"), symbol="SPXW_7525P",
                                  idempotency_key="stop:e1:PUT")
    resting_id = asyncio.run(broker.submit(stop_intent))
    wd.resting_stop_ids[("e1", "PUT")] = resting_id

    orig_submit = broker.submit

    async def submit_and_race(intent):
        if intent.kind == "escalation":
            broker.fill_stop(resting_id)  # the resting stop wins the race, THIS beat
        return await orig_submit(intent)
    broker.submit = submit_and_race  # type: ignore[method-assign]

    asyncio.run(wd.escalate(entry_id="e1", side="PUT", mark_at_breach=D("3.90"),
                           ask=D("3.95"), symbol="SPXW_7525P"))

    assert any("raced the resting stop" in msg for _, msg, _ in alerts.calls)
    assert any(level == "critical" for level, _, _ in alerts.calls)
    # the double-fill is flagged, not silently folded into a normal escalation
    stopped = [e for e in events if isinstance(e, ShortStopped)]
    assert stopped and stopped[0].initiator == "watchdog_escalation"


def test_escalation_with_no_race_cancels_the_resting_stop_as_before():
    """Sanity check: the ordinary escalation path (no race) is untouched — the
    resting stop is cancelled, no double-fill alert fires."""
    from meic.application.order_intent import protective_stop

    clock = FakeClock(SCHEDULED)
    broker = LiveShapedBroker(clock, fill_delay=10_000.0)
    events: list = []
    alerts = _Alerts()
    wd = StopWatchdog(broker=broker, alerts=alerts, events=events)

    stop_intent = protective_stop(entry_id="e2", right="C", contracts=1,
                                  trigger=D("3.80"), symbol="SPXW_7550C",
                                  idempotency_key="stop:e2:CALL")
    resting_id = asyncio.run(broker.submit(stop_intent))
    wd.resting_stop_ids[("e2", "CALL")] = resting_id

    asyncio.run(wd.escalate(entry_id="e2", side="CALL", mark_at_breach=D("3.90"),
                           ask=D("3.95"), symbol="SPXW_7550C"))

    assert not any("raced the resting stop" in msg for _, msg, _ in alerts.calls)
    working = asyncio.run(broker.working_orders())
    assert not any(str(getattr(o, "id", "")) == resting_id for o in working)
