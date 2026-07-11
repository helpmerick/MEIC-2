"""TC-EOD-03 (EOD-03): after settlement/close, zero working orders remain; an
uncancellable order produces a named critical alert (the day does not silently
end on a leftover)."""
import asyncio
from dataclasses import dataclass

from meic.application.eod_sweep import EndOfDaySweep


@dataclass
class Order:
    order_id: str
    status: str  # WORKING | CANCELLED


class SweepBroker:
    """A broker whose orders cancel normally, except any id in `stuck` which
    refuses cancellation and stays WORKING (an uncancellable order)."""

    def __init__(self, orders, stuck=()):
        self._orders = {o.order_id: o for o in orders}
        self._stuck = set(stuck)

    async def working_orders(self):
        return [o for o in self._orders.values() if o.status == "WORKING"]

    async def cancel(self, id):
        o = self._orders.get(id)
        if o and id not in self._stuck:
            o.status = "CANCELLED"
            return {"result": "cancelled"}
        return {"result": "terminal"}  # broker refused — stays WORKING

    async def fills_since(self, cursor):
        return []  # this fixture never scripts a race-fill scenario


class RecordingAlerts:
    def __init__(self):
        self.alerts = []

    def alert(self, level, message, **context):
        self.alerts.append((level, message, context))


def test_tc_eod_03_all_cancelled_leaves_zero_working():
    """The happy path: every resting stop cancels; the sweep confirms zero
    working orders remain and raises no alert."""
    broker = SweepBroker([Order("stopP", "WORKING"), Order("stopC", "WORKING")])
    alerts = RecordingAlerts()

    result = asyncio.run(EndOfDaySweep(broker, alerts).sweep())

    assert asyncio.run(broker.working_orders()) == []  # zero working orders remain
    assert set(result.cancelled) == {"stopP", "stopC"}
    assert result.uncancellable == []
    assert result.clean is True
    assert alerts.alerts == []


def test_tc_eod_03_uncancellable_order_raises_named_critical_alert():
    """An order the broker will not cancel is named in a critical alert, and the
    sweep reports the day as not clean."""
    broker = SweepBroker(
        [Order("stopP", "WORKING"), Order("stuckC", "WORKING")], stuck={"stuckC"})
    alerts = RecordingAlerts()

    result = asyncio.run(EndOfDaySweep(broker, alerts).sweep())

    assert result.cancelled == ["stopP"]
    assert result.uncancellable == ["stuckC"]
    assert result.clean is False  # day-complete gate must not pass

    assert len(alerts.alerts) == 1
    level, message, context = alerts.alerts[0]
    assert level == "critical"
    assert "stuckC" in message and context["order_id"] == "stuckC"  # named
